"""Behavior tests for the reproducible AK3 build pipeline."""

from pathlib import Path
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build_ak3 import (
    ak3_archive_name,
    adapt_susfs_legacy_header,
    apply_patches,
    cached_git_matches,
    ccache_environment,
    ccache_statistics,
    configure_copied_resukisu_driver,
    copy_resukisu_driver,
    create_ak3_package_directory,
    create_run_directory,
    FULL_BUILD_CONFIG,
    FULL_BUILD_EXPECTED_CONFIG,
    kernel_build_commands,
    latest_remote_head_commit,
    load_manifest,
    main,
    parse_args,
    pinned_fetch_commands,
    render_build_info,
    resukisu_version_code,
    run,
    select_integration_patch,
    package_ak3,
    update_resukisu_revision,
    validate_config_preservation,
    write_patch_files,
)


class ManifestTests(unittest.TestCase):
    def test_load_manifest_rejects_missing_required_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"schema_version": 1}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "kernel"):
                load_manifest(path)

    def test_project_manifest_declares_ordered_patch_targets(self):
        manifest = load_manifest(Path(__file__).parents[1] / "build_manifest.json")

        self.assertEqual(
            [(patch["file"], patch["target"]) for patch in manifest["patches"]],
            [
                ("03-ntsync-base.patch", "kernel"),
                ("06-ntsync-linux-4.19-compat.patch", "kernel"),
                ("01-kernel-integration.patch", "kernel"),
                ("02-resukisu-susfs-legacy.patch", "resukisu"),
            ],
        )

    def test_project_manifest_requires_full_resukisu_history_for_versioning(self):
        manifest = load_manifest(Path(__file__).parents[1] / "build_manifest.json")

        self.assertTrue(manifest["resukisu"]["full_history"])

    def test_defaults_allow_a_full_build_without_stage_selection(self):
        with patch.object(sys, "argv", ["build_ak3.py"]):
            args = parse_args()

        script_root = Path(__file__).parents[1]
        self.assertEqual(args.manifest, script_root / "build_manifest.json")
        self.assertEqual(args.output_dir, script_root / "outputs/ak3")
        self.assertEqual(args.cache_dir, script_root / ".cache/ak3-builder")
        self.assertEqual(args.ccache_dir, script_root / ".cache/ak3-builder/ccache")
        self.assertEqual(args.work_dir, Path("/tmp/ak3"))
        self.assertFalse(hasattr(args, "stage"))


class FullBuildTests(unittest.TestCase):
    def test_ak3_archive_name_uses_numeric_resukisu_version(self):
        self.assertEqual(
            ak3_archive_name("35045"),
            "elish_Infinity-X_4.19_RESUKI_SUSFS_35045_AK3.zip",
        )

    def test_resukisu_version_code_uses_kbuild_formula_and_git_history(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            kbuild = source / "kernel" / "Kbuild"
            kbuild.parent.mkdir()
            kbuild.write_text(
                "KSU_VERSION := $(shell expr 30000 + $(KSU_LOCAL_VERSION) + 700)\n",
                encoding="utf-8",
            )

            with patch("build_ak3.run", return_value="4345\n"):
                self.assertEqual(resukisu_version_code(source), "35045")

    def test_resukisu_version_code_rejects_an_unknown_kbuild_formula(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            kbuild = source / "kernel" / "Kbuild"
            kbuild.parent.mkdir()
            kbuild.write_text("KSU_VERSION := unknown\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "KSU_VERSION formula"):
                resukisu_version_code(source)


class ReSukiSUUpdateTests(unittest.TestCase):
    def test_latest_remote_head_commit_reads_a_full_sha(self):
        expected = "a" * 40

        with patch("build_ak3.run", return_value=f"{expected}\tHEAD\n"):
            self.assertEqual(
                latest_remote_head_commit("https://example.invalid/ReSukiSU.git"), expected
            )

    def test_latest_remote_head_commit_rejects_malformed_output(self):
        with patch("build_ak3.run", return_value="not-a-commit\tHEAD\n"):
            with self.assertRaisesRegex(RuntimeError, "latest ReSukiSU commit"):
                latest_remote_head_commit("https://example.invalid/ReSukiSU.git")

    def test_update_resukisu_revision_persists_a_new_remote_commit(self):
        previous = "b" * 40
        latest = "c" * 40
        manifest_text = (
            '{\n'
            '  "schema_version": 1,\n'
            '  "resukisu": {\n'
            '    "url": "https://example.invalid/ReSukiSU.git",\n'
            f'    "commit": "{previous}"\n'
            '  }\n'
            '}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build_manifest.json"
            path.write_text(manifest_text, encoding="utf-8")
            manifest = json.loads(manifest_text)

            with patch("build_ak3.latest_remote_head_commit", return_value=latest):
                updated = update_resukisu_revision(path, manifest)

            self.assertEqual(updated["resukisu"]["commit"], latest)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["resukisu"]["commit"], latest)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_update_resukisu_revision_does_not_rewrite_an_current_manifest(self):
        current = "d" * 40
        manifest_text = (
            '{\n'
            '  "schema_version": 1,\n'
            '  "resukisu": {\n'
            '    "url": "https://example.invalid/ReSukiSU.git",\n'
            f'    "commit": "{current}"\n'
            '  }\n'
            '}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build_manifest.json"
            path.write_text(manifest_text, encoding="utf-8")
            manifest = json.loads(manifest_text)

            with patch("build_ak3.latest_remote_head_commit", return_value=current):
                updated = update_resukisu_revision(path, manifest)

            self.assertEqual(updated, manifest)
            self.assertEqual(path.read_text(encoding="utf-8"), manifest_text)


class FullBuildConfigurationTests(unittest.TestCase):
    def test_full_build_configuration_enables_all_feature_groups(self):
        self.assertEqual(FULL_BUILD_CONFIG["CONFIG_KSU"], "y")
        self.assertEqual(FULL_BUILD_CONFIG["CONFIG_KSU_SUSFS"], "y")
        self.assertEqual(FULL_BUILD_CONFIG["CONFIG_NTSYNC"], "y")
        self.assertEqual(FULL_BUILD_CONFIG["CONFIG_TCP_CONG_BBR"], "y")

    def test_full_build_integration_patch_keeps_all_supported_hunks(self):
        manifest = load_manifest(Path(__file__).parents[1] / "build_manifest.json")
        integration = next(patch for patch in manifest["patches"] if patch["id"] == "kernel-integration")
        patch_path = Path(__file__).parents[1] / "patches" / integration["file"]
        contents = patch_path.read_text(encoding="utf-8")

        selected = select_integration_patch(contents)

        self.assertIn("ksu_handle_execveat", selected)
        self.assertIn("susfs_spoof_uname", selected)
        self.assertIn("config NTSYNC", selected)
        self.assertIn("rcu_trace_lock_map", selected)

    def test_config_validation_rejects_changes_outside_full_build(self):
        baseline = {"CONFIG_KSU": "n", "CONFIG_QCOM_WATCHDOG_V2": "y"}
        final = {"CONFIG_KSU": "y", "CONFIG_QCOM_WATCHDOG_V2": "n"}

        with self.assertRaisesRegex(RuntimeError, "CONFIG_QCOM_WATCHDOG_V2"):
            validate_config_preservation(baseline, final, {"CONFIG_KSU"})

    def test_full_build_allows_only_its_expected_kconfig_side_effects(self):
        derived_values = {
            "CONFIG_DEFAULT_TCP_CONG": '"bbr"',
            "CONFIG_IP_SET_MAX": "256",
            "CONFIG_NF_NAT_IPV6": "y",
            "CONFIG_NF_NAT_MASQUERADE_IPV6": "y",
            "CONFIG_POSIX_MQUEUE_SYSCTL": "y",
            "CONFIG_SYSVIPC_COMPAT": "y",
            "CONFIG_SYSVIPC_SYSCTL": "y",
            "CONFIG_TCP_CONG_BIC": "m",
            "CONFIG_TCP_CONG_HTCP": "m",
            "CONFIG_TCP_CONG_WESTWOOD": "m",
        }
        baseline = {
            **{name: "n" for name in derived_values},
            "CONFIG_DEFAULT_TCP_CONG": '"cubic"',
            "CONFIG_QCOM_WATCHDOG_V2": "y",
        }
        final = {**baseline, **derived_values}

        expected_values = FULL_BUILD_EXPECTED_CONFIG
        self.assertEqual(
            {name: expected_values[name] for name in derived_values}, derived_values
        )
        validate_config_preservation(baseline, final, set(expected_values))

        final["CONFIG_QCOM_WATCHDOG_V2"] = "n"
        with self.assertRaisesRegex(RuntimeError, "CONFIG_QCOM_WATCHDOG_V2"):
            validate_config_preservation(baseline, final, set(expected_values))

    def test_full_build_omits_symbols_rejected_by_target_kconfig(self):

        self.assertFalse(
            {
                "CONFIG_FW_LOADER_COMPRESS",
                "CONFIG_NETFILTER_XT_TARGET_MASQUERADE",
                "CONFIG_NF_CONNTRACK_IPV4",
                "CONFIG_NF_CONNTRACK_NETLINK",
            }
            & FULL_BUILD_CONFIG.keys()
        )

    def test_full_build_metadata_and_archive_name_identify_resukisu_version(self):
        info = render_build_info("base", "patched", "unsupported", "35045")

        self.assertIn("ReSukiSU version code: 35045", info)
        self.assertIn("ReSukiSU + SusFS", info)
        self.assertEqual(ak3_archive_name("35045"), "elish_Infinity-X_4.19_RESUKI_SUSFS_35045_AK3.zip")


class PatchWriterTests(unittest.TestCase):
    def test_write_patch_files_creates_manifest_ordered_diffs(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = write_patch_files(
                Path(directory),
                [{"id": "example", "contents": "diff --git a/a b/a\n"}],
            )

            self.assertEqual([path.name for path in paths], ["01-example.patch"])
            self.assertIn("diff --git", paths[0].read_text(encoding="utf-8"))

    def test_apply_patches_checks_before_modifying_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)

            with self.assertRaisesRegex(RuntimeError, "command failed"):
                apply_patches(source, [{"id": "invalid", "contents": "not a patch"}])

            self.assertFalse((source / "invalid").exists())


class Ak3PackagingTests(unittest.TestCase):
    def test_package_directory_does_not_collide_with_a_previous_failed_package(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            previous = output_dir / ".ak3-package-previous"
            previous.mkdir()

            package_dir = create_ak3_package_directory(output_dir)

            self.assertTrue(previous.is_dir())
            self.assertTrue(package_dir.is_dir())
            self.assertNotEqual(package_dir, previous)
            self.assertTrue(package_dir.name.startswith(".ak3-package-"))

    def test_package_ak3_archives_from_a_unique_precreated_package_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anykernel = root / "anykernel"
            (anykernel / "tools").mkdir(parents=True)
            (anykernel / "tools" / "ak3-core.sh").write_text("\n", encoding="utf-8")
            patched_boot = root / "patched-boot.img"
            patched_boot.write_bytes(b"boot")
            output_dir = root / "output"
            (output_dir / "previous-failed-package").mkdir(parents=True)
            manifest = {
                "toolchain": {"revision": "r563880c"},
                "features": {"bbg": {"status": "unsupported"}},
            }

            archive = package_ak3(
                anykernel,
                patched_boot,
                manifest,
                output_dir,
                "base-hash",
                "35045",
            )

            self.assertTrue(archive.is_file())
            self.assertEqual(subprocess.run(["unzip", "-tq", str(archive)]).returncode, 0)
            self.assertEqual(list(output_dir.glob(".ak3-package-*")), [])


class CommandRunnerTests(unittest.TestCase):
    def test_run_live_streams_subprocess_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run(
                [sys.executable, "-c", "print('live-output')"],
                live=True,
            )

        self.assertEqual(result, "live-output\n")
        self.assertIn("[exec]", output.getvalue())
        self.assertIn("live-output", output.getvalue())
        self.assertIn("[done]", output.getvalue())


class SusfsCompatibilityTests(unittest.TestCase):
    def test_legacy_header_gets_re_sukisu_magic_once(self):
        with tempfile.TemporaryDirectory() as directory:
            header = Path(directory) / "susfs_def.h"
            header.write_text("#include <linux/bits.h>\n", encoding="utf-8")

            adapt_susfs_legacy_header(header)
            adapt_susfs_legacy_header(header)

            self.assertEqual(header.read_text(encoding="utf-8").count("SUSFS_MAGIC"), 1)


class NtsyncCompatibilityTests(unittest.TestCase):
    def test_linux_419_patch_falls_back_when_lockdep_state_api_is_unavailable(self):
        patch_text = (
            Path(__file__).parents[1] / "patches" / "06-ntsync-linux-4.19-compat.patch"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "+#ifndef lockdep_is_held\n+#define lockdep_is_held(lock) (1)\n+#endif",
            patch_text,
        )


class ReSukiSUDriverTests(unittest.TestCase):
    def test_copy_resukisu_driver_keeps_relative_uapi_link_resolvable(self):
        with tempfile.TemporaryDirectory() as directory:
            kernel = Path(directory)
            source_driver = kernel / "KernelSU" / "kernel"
            source_uapi = kernel / "KernelSU" / "uapi"
            (source_driver / "include").mkdir(parents=True)
            source_uapi.mkdir(parents=True)
            (source_uapi / "app_profile.h").write_text("#pragma once\n", encoding="utf-8")
            (source_driver / "include" / "uapi").symlink_to("../../uapi")

            copy_resukisu_driver(kernel)

            self.assertTrue(
                (kernel / "drivers" / "kernelsu" / "include" / "uapi" / "app_profile.h").is_file()
            )

    def test_copied_driver_uses_original_resukisu_git_repository_for_versioning(self):
        with tempfile.TemporaryDirectory() as directory:
            kernel = Path(directory)
            source_driver = kernel / "KernelSU" / "kernel"
            source_driver.mkdir(parents=True)
            (source_driver / "Kbuild").write_text(
                "KSU_SRC := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))\n",
                encoding="utf-8",
            )
            (kernel / "KernelSU" / "uapi").mkdir()

            copy_resukisu_driver(kernel)
            configure_copied_resukisu_driver(kernel)

            driver_kbuild = (kernel / "drivers" / "kernelsu" / "Kbuild").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "KSU_SRC := $(realpath $(srctree)/KernelSU/kernel)", driver_kbuild
            )


class CacheTests(unittest.TestCase):
    def test_pinned_fetch_commands_avoid_full_history_clone(self):
        checkout = Path("cache/lineage-sm8250")
        commands = pinned_fetch_commands(
            "https://example.invalid/kernel.git", "deadbeef", checkout
        )

        self.assertEqual(commands[0], ["git", "init", "--quiet", str(checkout)])
        self.assertEqual(
            commands[1],
            ["git", "-C", str(checkout), "remote", "add", "origin", "https://example.invalid/kernel.git"],
        )
        self.assertEqual(
            commands[2],
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--no-tags",
                "--depth=1",
                "--no-progress",
                "origin",
                "deadbeef",
            ],
        )
        self.assertEqual(
            commands[3], ["git", "-C", str(checkout), "checkout", "--detach", "--quiet", "deadbeef"]
        )
        self.assertNotIn("clone", " ".join(" ".join(command) for command in commands))

    def test_pinned_fetch_commands_can_request_full_history(self):
        checkout = Path("cache/resukisu")
        commands = pinned_fetch_commands(
            "https://example.invalid/resukisu.git", "deadbeef", checkout, full_history=True
        )

        self.assertNotIn("--depth=1", commands[2])

    def test_cached_git_matches_rejects_non_repository_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()

            self.assertFalse(
                cached_git_matches(checkout, "https://example.invalid/repo.git", "deadbeef")
            )

    def test_cached_git_matches_accepts_exact_origin_and_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repository,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
            (repository / "marker").write_text("ok", encoding="utf-8")
            subprocess.run(["git", "add", "marker"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://example.invalid/repo.git"],
                cwd=repository,
                check=True,
            )
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True
            ).strip()

            self.assertTrue(cached_git_matches(repository, "https://example.invalid/repo.git", commit))
            self.assertFalse(cached_git_matches(repository, "https://example.invalid/other.git", commit))

    def test_cached_git_matches_rejects_shallow_repository_when_full_history_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            origin = Path(directory) / "origin"
            clone = Path(directory) / "clone"
            origin.mkdir()
            subprocess.run(["git", "init"], cwd=origin, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=origin, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=origin, check=True)
            (origin / "marker").write_text("one", encoding="utf-8")
            subprocess.run(["git", "add", "marker"], cwd=origin, check=True)
            subprocess.run(["git", "commit", "-m", "first"], cwd=origin, check=True)
            (origin / "marker").write_text("two", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "second"], cwd=origin, check=True)
            subprocess.run(["git", "clone", "--no-local", "--depth=1", str(origin), str(clone)], check=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone, text=True).strip()

            self.assertTrue(cached_git_matches(clone, str(origin), commit))
            self.assertFalse(cached_git_matches(clone, str(origin), commit, full_history=True))


class SourceCompatibilityTests(unittest.TestCase):
    def test_build_script_does_not_gate_on_kernel_source_revision(self):
        script = (Path(__file__).parents[1] / "build_ak3.py").read_text(encoding="utf-8")

        self.assertNotIn("validate_source_revision", script)

    def test_create_run_directory_uses_unique_child_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = create_run_directory(root)
            second = create_run_directory(root)

            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())
            self.assertNotEqual(first, second)

    def test_successful_main_retains_workdir_only_when_requested(self):
        for keep_workdir, should_remain in ((False, False), (True, True)):
            with self.subTest(keep_workdir=keep_workdir), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                boot_img = root / "boot.img"
                boot_img.write_bytes(b"boot")
                run_dir = root / "run"
                run_dir.mkdir()
                args = SimpleNamespace(
                    manifest=root / "manifest.json",
                    boot_img=boot_img,
                    cache_dir=root / "cache",
                    local_root=root,
                    ccache_dir=root / "ccache",
                    output_dir=root / "output",
                    work_dir=root,
                    patches_only=False,
                    keep_workdir=keep_workdir,
                )

                with (
                    patch("build_ak3.parse_args", return_value=args),
                    patch(
                        "build_ak3.load_manifest",
                        return_value={"toolchain": {}, "anykernel3": {}},
                    ),
                    patch(
                        "build_ak3.update_resukisu_revision",
                        side_effect=lambda _path, manifest: manifest,
                    ),
                    patch("build_ak3.create_run_directory", return_value=run_dir),
                    patch("build_ak3.prepare_patched_kernel", return_value=root / "kernel"),
                    patch("build_ak3.extract_boot_config", return_value=object()),
                    patch(
                        "build_ak3.ensure_git_source",
                        side_effect=[root / "toolchain", root / "anykernel"],
                    ),
                    patch("build_ak3.build_kernel", return_value=root / "Image"),
                    patch("build_ak3.repack_boot", return_value=root / "patched-boot.img"),
                    patch("build_ak3.resukisu_version_code", return_value="35045"),
                    patch("build_ak3.package_ak3", return_value=root / "package.zip"),
                ):
                    self.assertEqual(main(), 0)

                self.assertEqual(run_dir.exists(), should_remain)


class BuildRenderingTests(unittest.TestCase):
    def test_kernel_build_commands_compile_image_and_modules(self):
        commands = kernel_build_commands(Path("source"), Path("out"), "ccache clang")

        self.assertEqual(commands[0][-1], "vendor/kona_defconfig")
        self.assertEqual(commands[-1][-2:], ["Image", "modules"])

    def test_kernel_build_commands_use_ccache_compiler(self):
        commands = kernel_build_commands(Path("source"), Path("out"), "/usr/bin/ccache clang")

        self.assertIn("CC=/usr/bin/ccache clang", commands[0])
        self.assertIn("CC=/usr/bin/ccache clang", commands[-1])

    def test_render_build_info_records_image_hashes_and_bbg_status(self):
        text = render_build_info("base", "patched", "unsupported", "35045")

        self.assertIn("Base image SHA-256: base", text)
        self.assertIn("Patched SHA-256: patched", text)
        self.assertIn("BBG status: unsupported", text)


class CcacheTests(unittest.TestCase):
    def test_ccache_environment_uses_requested_cache_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "cache"
            source = Path(directory) / "source"
            with patch("build_ak3.shutil.which", return_value="/usr/bin/ccache"):
                env = ccache_environment({"PATH": "/bin"}, cache_dir, source)

        self.assertEqual(env["CC"], "/usr/bin/ccache clang")
        self.assertEqual(env["CXX"], "/usr/bin/ccache clang++")
        self.assertEqual(env["CCACHE_DIR"], str(cache_dir.resolve()))
        self.assertEqual(env["CCACHE_BASEDIR"], str(source.resolve()))
        self.assertEqual(env["CCACHE_NOHASHDIR"], "true")

    def test_ccache_environment_rejects_missing_host_binary(self):
        with patch("build_ak3.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "ccache"):
                ccache_environment({}, Path("cache"), Path("source"))

    def test_ccache_statistics_select_requested_cache_directory(self):
        with patch("build_ak3.shutil.which", return_value="/usr/bin/ccache"):
            with patch("build_ak3.run", return_value="Cache size: 0.0 kB\n") as run_mock:
                statistics = ccache_statistics(Path("cache"))

        self.assertEqual(statistics, "Cache size: 0.0 kB")
        run_mock.assert_called_once_with(
            ["/usr/bin/ccache", "--dir", str(Path("cache").resolve()), "--show-stats"]
        )
