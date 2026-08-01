#!/usr/bin/env python3
"""Build a patched elish boot image and verified AnyKernel3 package.

The pipeline always works from pinned clean source copies. Compatibility diffs
are written into a disposable run directory before being checked and applied.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any


REQUIRED_SECTIONS = {
    "kernel",
    "resukisu",
    "susfs",
    "ntsync",
    "anykernel3",
    "toolchain",
    "device",
    "features",
    "patches",
}
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
SCRIPT_ROOT = SCRIPT_DIRECTORY


RESUKISU_CONFIG = {
    "CONFIG_KSU": "y",
    "CONFIG_KSU_DEBUG": "n",
    "CONFIG_KSU_TOOLKIT_SUPPORT": "n",
    "CONFIG_KSU_FULL_NAME_FORMAT": '"%TAG_NAME%-%COMMIT_SHA%@%REPO_NAME%"',
    "CONFIG_KSU_DISABLE_MANAGER": "n",
    "CONFIG_KSU_DISABLE_POLICY": "n",
    "CONFIG_KSU_MULTI_MANAGER_SUPPORT": "y",
    "CONFIG_KSU_TRACEPOINT_HOOK": "n",
    "CONFIG_KSU_MANUAL_HOOK": "y",
    "CONFIG_KSU_MANUAL_HOOK_AUTO_SETUID_HOOK": "n",
    "CONFIG_KSU_MANUAL_HOOK_AUTO_INITRC_HOOK": "n",
    "CONFIG_KSU_MANUAL_HOOK_AUTO_INPUT_HOOK": "n",
}

SUSFS_CONFIG = {
    "CONFIG_KSU_MANUAL_HOOK": "n",
    "CONFIG_KSU_SUSFS": "y",
    "CONFIG_KSU_SUSFS_SUS_PATH": "y",
    "CONFIG_KSU_SUSFS_SUS_MOUNT": "y",
    "CONFIG_KSU_SUSFS_SUS_KSTAT": "y",
    "CONFIG_KSU_SUSFS_SPOOF_UNAME": "y",
    "CONFIG_KSU_SUSFS_ENABLE_LOG": "y",
    "CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS": "y",
    "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG": "y",
    "CONFIG_KSU_SUSFS_OPEN_REDIRECT": "y",
    "CONFIG_KSU_SUSFS_SUS_MAP": "n",
}

NTSYNC_CONFIG = {
    "CONFIG_NTSYNC": "y",
}

FULL_CONFIG = {
    "CONFIG_NET_SCH_FQ": "y",
    "CONFIG_TCP_CONG_ADVANCED": "y",
    "CONFIG_TCP_CONG_BBR": "y",
    "CONFIG_DEFAULT_BBR": "y",
    "CONFIG_FUSE_BPF": "y",
    "CONFIG_IP_SET": "y",
    "CONFIG_IP_SET_BITMAP_IP": "y",
    "CONFIG_IP_SET_BITMAP_IPMAC": "y",
    "CONFIG_IP_SET_BITMAP_PORT": "y",
    "CONFIG_IP_SET_HASH_IP": "y",
    "CONFIG_IP_SET_HASH_IPMARK": "y",
    "CONFIG_IP_SET_HASH_IPPORT": "y",
    "CONFIG_IP_SET_HASH_IPPORTIP": "y",
    "CONFIG_IP_SET_HASH_IPPORTNET": "y",
    "CONFIG_IP_SET_HASH_IPMAC": "y",
    "CONFIG_IP_SET_HASH_MAC": "y",
    "CONFIG_IP_SET_HASH_NETPORTNET": "y",
    "CONFIG_IP_SET_HASH_NET": "y",
    "CONFIG_IP_SET_HASH_NETNET": "y",
    "CONFIG_IP_SET_HASH_NETPORT": "y",
    "CONFIG_IP_SET_HASH_NETIFACE": "y",
    "CONFIG_IP_SET_LIST_SET": "y",
    "CONFIG_NETFILTER_XT_TARGET_HL": "y",
    "CONFIG_IP_NF_TARGET_TTL": "y",
    "CONFIG_IP6_NF_NAT": "y",
    "CONFIG_IP6_NF_TARGET_MASQUERADE": "y",
    "CONFIG_ANDROID_VENDOR_HOOKS": "y",
    "CONFIG_SYSCTL": "y",
    "CONFIG_SYSVIPC": "y",
    "CONFIG_POSIX_MQUEUE": "y",
    "CONFIG_NAMESPACES": "y",
    "CONFIG_PID_NS": "y",
    "CONFIG_UTS_NS": "y",
    "CONFIG_IPC_NS": "y",
    "CONFIG_SECCOMP": "y",
    "CONFIG_SECCOMP_FILTER": "y",
    "CONFIG_CGROUPS": "y",
    "CONFIG_CGROUP_DEVICE": "y",
    "CONFIG_CGROUP_PIDS": "y",
    "CONFIG_MEMCG": "y",
    "CONFIG_CGROUP_SCHED": "y",
    "CONFIG_FAIR_GROUP_SCHED": "y",
    "CONFIG_CGROUP_FREEZER": "y",
    "CONFIG_CGROUP_NET_PRIO": "y",
    "CONFIG_DEVTMPFS": "y",
    "CONFIG_OVERLAY_FS": "y",
    "CONFIG_TMPFS_POSIX_ACL": "y",
    "CONFIG_TMPFS_XATTR": "y",
    "CONFIG_FW_LOADER": "y",
    "CONFIG_FW_LOADER_USER_HELPER": "y",
    "CONFIG_NET_NS": "y",
    "CONFIG_VETH": "y",
    "CONFIG_BRIDGE": "y",
    "CONFIG_NETFILTER": "y",
    "CONFIG_BRIDGE_NETFILTER": "y",
    "CONFIG_NETFILTER_ADVANCED": "y",
    "CONFIG_NF_CONNTRACK": "y",
    "CONFIG_IP_NF_IPTABLES": "y",
    "CONFIG_IP_NF_FILTER": "y",
    "CONFIG_NF_NAT": "y",
    "CONFIG_NF_TABLES": "y",
    "CONFIG_IP_NF_TARGET_MASQUERADE": "y",
    "CONFIG_NETFILTER_XT_TARGET_TCPMSS": "y",
    "CONFIG_NETFILTER_XT_MATCH_ADDRTYPE": "y",
    "CONFIG_NF_NAT_REDIRECT": "y",
    "CONFIG_IP_ADVANCED_ROUTER": "y",
    "CONFIG_IP_MULTIPLE_TABLES": "y",
    "CONFIG_NF_NAT_IPV4": "y",
    "CONFIG_IP_NF_NAT": "y",
    "CONFIG_ANDROID_PARANOID_NETWORK": "n",
    "CONFIG_USER_NS": "y",
}

# These values are selected or defaulted by Kconfig when FULL_CONFIG is enabled.
FULL_CONFIG_DERIVED_VALUES = {
    "CONFIG_DEFAULT_TCP_CONG": '"bbr"',
    "CONFIG_IP_SET_MAX": "256",
    "CONFIG_NF_NAT_IPV6": "y",
    "CONFIG_NF_NAT_MASQUERADE_IPV6": "y",
    "CONFIG_POSIX_MQUEUE_SYSCTL": "y",
    "CONFIG_SYSVIPC_COMPAT": "y",
    "CONFIG_SYSVIPC_SYSCTL": "y",
    "CONFIG_SDCARD_FS": "n",
    "CONFIG_TCP_CONG_BIC": "m",
    "CONFIG_TCP_CONG_HTCP": "m",
    "CONFIG_TCP_CONG_WESTWOOD": "m",
}


def merged_config(*groups: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for group in groups:
        values.update(group)
    return values


FULL_BUILD_CONFIG = merged_config(RESUKISU_CONFIG, SUSFS_CONFIG, NTSYNC_CONFIG, FULL_CONFIG)
FULL_BUILD_EXPECTED_CONFIG = merged_config(FULL_BUILD_CONFIG, FULL_CONFIG_DERIVED_VALUES)
FULL_BUILD_DESCRIPTION = "ReSukiSU + SusFS + NTSync + networking/DroidSpaces"


def default_input_path(name: str, fallback: Path) -> Path:
    """Prefer inputs beside this script, with a compatibility fallback."""
    candidate = SCRIPT_ROOT / name
    return candidate if candidate.exists() else fallback


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    live: bool = False,
) -> str:
    """Run a checked command, optionally forwarding output while it runs."""
    if not live:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError as error:
            output = error.stdout or "(no command output captured)"
            raise RuntimeError(
                f"command failed ({error.returncode}): {shlex.join(command)}\n{output}"
            ) from error
        return completed.stdout

    location = f" cwd={cwd}" if cwd else ""
    command_text = shlex.join(command)
    print(f"[exec]{location} $ {command_text}", flush=True)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    output: list[str] = []
    assert process.stdout is not None
    try:
        for line in process.stdout:
            output.append(line)
            print(line, end="", flush=True)
    finally:
        process.stdout.close()
    return_code = process.wait()
    combined = "".join(output)
    elapsed = time.monotonic() - started
    if return_code:
        if combined and not combined.endswith("\n"):
            print(flush=True)
        raise RuntimeError(
            f"command failed ({return_code}): {command_text}\n"
            f"{combined or '(no command output captured)'}"
        )
    if combined and not combined.endswith("\n"):
        print(flush=True)
    print(f"[done] {command_text} ({elapsed:.1f}s)", flush=True)
    return combined


def load_manifest(path: Path) -> dict[str, Any]:
    """Load and validate the version-locked build manifest."""
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = sorted(REQUIRED_SECTIONS - data.keys())
    if missing:
        raise ValueError(f"manifest is missing required sections: {', '.join(missing)}")
    if data.get("schema_version") != 1:
        raise ValueError("unsupported manifest schema_version")
    return data


def latest_remote_head_commit(url: str) -> str:
    """Return the full commit SHA at a source repository's default branch."""
    output = run(["git", "ls-remote", url, "HEAD"])
    match = re.fullmatch(r"([0-9a-f]{40})\tHEAD\n?", output)
    if not match:
        raise RuntimeError("unable to determine latest ReSukiSU commit from remote HEAD")
    return match.group(1)


def update_resukisu_revision(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Persist the current ReSukiSU default-branch revision before a build."""
    source = manifest["resukisu"]
    current = source["commit"]
    latest = latest_remote_head_commit(source["url"])
    if latest == current:
        print(f"ReSukiSU is current at {current}")
        return manifest

    source["commit"] = latest
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(f"ReSukiSU updated: {current} -> {latest}")
    return manifest


def cached_git_matches(path: Path, url: str, commit: str, full_history: bool = False) -> bool:
    """Return true only when a cache checkout has the exact expected origin and HEAD."""
    if not (path / ".git").is_dir():
        return False
    try:
        origin = run(["git", "remote", "get-url", "origin"], cwd=path).strip()
        head = run(["git", "rev-parse", "HEAD"], cwd=path).strip()
        shallow = run(["git", "rev-parse", "--is-shallow-repository"], cwd=path).strip()
    except subprocess.CalledProcessError:
        return False
    return origin == url and head == commit and (not full_history or shallow == "false")


def create_run_directory(work_root: Path) -> Path:
    """Create one disposable run directory without changing existing runs."""
    work_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=work_root))


def pinned_fetch_commands(
    url: str, commit: str, destination: Path, full_history: bool = False
) -> list[list[str]]:
    """Return commands for a quiet, exact checkout with the requested history depth."""
    fetch = ["git", "-C", str(destination), "fetch", "--no-tags", "--no-progress", "origin", commit]
    if not full_history:
        fetch[5:5] = ["--depth=1"]
    return [
        ["git", "init", "--quiet", str(destination)],
        ["git", "-C", str(destination), "remote", "add", "origin", url],
        fetch,
        ["git", "-C", str(destination), "checkout", "--detach", "--quiet", commit],
    ]


def ensure_git_source(spec: dict[str, str], cache_root: Path, local_root: Path) -> Path:
    """Return an exact pinned checkout, preferring a matching local directory."""
    source_id = spec["id"]
    url = spec["url"]
    commit = spec["commit"]
    full_history = spec.get("full_history", False)
    relative_dir = Path(spec.get("local_dir", source_id))
    local_candidates = [local_root / relative_dir]
    current_dir = Path.cwd() / relative_dir
    if current_dir not in local_candidates:
        local_candidates.append(current_dir)
    for local_dir in local_candidates:
        if cached_git_matches(local_dir, url, commit, full_history=full_history):
            print(f"[source] reuse local {source_id}: {local_dir}")
            return local_dir

    cached_dir = cache_root / "git" / f"{source_id}-{commit[:12]}"
    if cached_git_matches(cached_dir, url, commit, full_history=full_history):
        print(f"[source] reuse cache {source_id}: {cached_dir}")
        return cached_dir

    if full_history and cached_git_matches(cached_dir, url, commit):
        print(f"[source] unshallow cache {source_id}: {cached_dir}")
        run(["git", "fetch", "--unshallow", "--no-tags", "--no-progress", "origin"], cwd=cached_dir, live=True)
        if cached_git_matches(cached_dir, url, commit, full_history=True):
            return cached_dir
        raise RuntimeError(f"unable to retrieve complete history for {source_id}")

    cached_dir.parent.mkdir(parents=True, exist_ok=True)
    if cached_dir.exists():
        raise RuntimeError(f"mismatched cache directory exists: {cached_dir}")
    print(f"[source] download {source_id} @ {commit} -> {cached_dir}")
    init, remote, fetch, checkout = pinned_fetch_commands(
        url, commit, cached_dir, full_history=full_history
    )
    run(init, live=True)
    run(remote, live=True)
    run(fetch, live=True)
    run(checkout, live=True)
    if not cached_git_matches(cached_dir, url, commit, full_history=full_history):
        raise RuntimeError(f"source verification failed: {source_id}")
    return cached_dir


def clone_clean_source(source: Path, destination: Path) -> Path:
    """Clone a clean disposable source tree from a verified checkout."""
    run(["git", "clone", "--progress", "--no-local", str(source), str(destination)], live=True)
    return destination


def write_patch_files(directory: Path, patches: list[dict[str, str]]) -> list[Path]:
    """Write manifest-ordered patch contents into an isolated run directory."""
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, patch in enumerate(patches, start=1):
        path = directory / f"{index:02d}-{patch['id']}.patch"
        path.write_text(patch["contents"], encoding="utf-8")
        paths.append(path)
    return paths


def apply_patches(source: Path, patches: list[dict[str, str]]) -> None:
    """Check every diff before applying any source modification from it."""
    paths = write_patch_files(source.parent / "patches", patches)
    for path in paths:
        run(["git", "apply", "--check", str(path)], cwd=source, live=True)
    for path in paths:
        run(["git", "apply", str(path)], cwd=source, live=True)


def patch_records(patches: list[dict[str, str]]) -> list[dict[str, str]]:
    """Load manifest-ordered, project-owned compatibility diffs."""
    records = []
    for patch_spec in patches:
        if "id" not in patch_spec or "file" not in patch_spec:
            raise ValueError("each patch record requires id and file")
        path = SCRIPT_DIRECTORY / "patches" / patch_spec["file"]
        if not path.is_file():
            raise RuntimeError(f"missing packaged patch: {path}")
        records.append(
            {"id": patch_spec["id"], "contents": path.read_text(encoding="utf-8")}
        )
    return records


def split_patch_by_file(contents: str) -> list[str]:
    """Split a Git patch into independently selectable per-file sections."""
    sections = re.split(r"(?=^diff --git )", contents, flags=re.MULTILINE)
    return [section for section in sections if section.startswith("diff --git ")]


def select_integration_patch(contents: str) -> str:
    """Return the complete integration patch excluding only device config hunks."""
    selected: list[str] = []
    for section in split_patch_by_file(contents):
        match = re.match(r"diff --git a/(.+?) b/", section)
        if not match:
            raise RuntimeError("unable to determine integration patch path")
        path = match.group(1)
        if path == "arch/arm64/configs/vendor/xiaomi/elish.config":
            continue
        selected.append(section)
    return "".join(selected)


def copy_file(source: Path, destination: Path) -> None:
    """Copy a required source file while creating only its parent directory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def prepare_external_sources(kernel: Path, resukisu: Path, susfs: Path) -> None:
    """Place the pinned ReSukiSU and SusFS sources for the full build."""
    # ReSukiSU uses UAPI symlinks; preserve them so its Git index and headers agree.
    shutil.copytree(resukisu, kernel / "KernelSU", dirs_exist_ok=False, symlinks=True)
    copy_file(susfs / "kernel_patches" / "fs" / "susfs.c", kernel / "fs" / "susfs.c")
    copy_file(
        susfs / "kernel_patches" / "include" / "linux" / "susfs.h",
        kernel / "include" / "linux" / "susfs.h",
    )
    copy_file(
        susfs / "kernel_patches" / "include" / "linux" / "susfs_def.h",
        kernel / "include" / "linux" / "susfs_def.h",
    )
    adapt_susfs_legacy_header(kernel / "include" / "linux" / "susfs_def.h")


def adapt_susfs_legacy_header(header: Path) -> None:
    """Add the ReSukiSU ioctl discriminator absent from SusFS v1.5.5."""
    text = header.read_text(encoding="utf-8")
    if "SUSFS_MAGIC" in text:
        return
    anchor = "#include <linux/bits.h>\n"
    if anchor not in text:
        raise RuntimeError("unable to locate SusFS header insertion point")
    header.write_text(
        text.replace(
            anchor,
            anchor
            + "\n/* Shared ioctl discriminator used by the ReSukiSU userspace client. */\n"
            + "#define SUSFS_MAGIC 0xFAFAFAFA\n",
            1,
        ),
        encoding="utf-8",
    )


def disable_copied_resukisu_submodule_guard(kernel: Path) -> None:
    """Remove the upstream submodule-only guard from the copied driver source."""
    kbuild = kernel / "drivers" / "kernelsu" / "Kbuild"
    text = kbuild.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"LOCAL_GIT_EXISTS :=.*?^endif\n\n",
        "# ReSukiSU is copied from a separately verified pinned checkout.\n\n",
        text,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    if count != 1:
        raise RuntimeError("unable to locate ReSukiSU submodule guard")
    kbuild.write_text(updated, encoding="utf-8")


def copy_resukisu_driver(kernel: Path) -> None:
    """Copy ReSukiSU sources while preserving its relative UAPI include link."""
    source = kernel / "KernelSU"
    shutil.copytree(
        source / "kernel",
        kernel / "drivers" / "kernelsu",
        dirs_exist_ok=True,
        symlinks=True,
    )
    shutil.copytree(
        source / "uapi",
        kernel / "drivers" / "uapi",
        dirs_exist_ok=True,
        symlinks=True,
    )


def configure_copied_resukisu_driver(kernel: Path) -> None:
    """Keep the copied driver tied to the original ReSukiSU Git history."""
    kbuild = kernel / "drivers" / "kernelsu" / "Kbuild"
    text = kbuild.read_text(encoding="utf-8")
    original = "KSU_SRC := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))"
    replacement = "KSU_SRC := $(realpath $(srctree)/KernelSU/kernel)"
    if text.count(original) != 1:
        raise RuntimeError("unable to locate copied ReSukiSU source path")
    kbuild.write_text(text.replace(original, replacement, 1), encoding="utf-8")


def assert_source_markers(kernel: Path) -> None:
    """Fail early when required full-build source changes are incomplete."""
    expected = {
        "KernelSU/kernel/Kbuild": "obj-$(CONFIG_KSU) += kernelsu.o",
        "drivers/kernelsu/Kbuild": "obj-$(CONFIG_KSU) += kernelsu.o",
        "drivers/uapi/app_profile.h": "__KSU_UAPI_APP_PROFILE_H",
        "fs/susfs.c": "susfs_init",
        "KernelSU/kernel/supercall/dispatch.c": "SUSFS_VERSION",
        "include/linux/susfs_def.h": "SUSFS_MAGIC",
        "drivers/misc/ntsync.c": "NTSYNC_NAME",
        "kernel/rcu/update.c": "rcu_trace_lock_map",
    }
    for relative, marker in expected.items():
        path = kernel / relative
        if not path.is_file() or marker not in path.read_text(encoding="utf-8"):
            raise RuntimeError(f"missing integration marker {marker} in {relative}")


def prepare_patched_kernel(
    manifest: dict[str, Any], cache_dir: Path, local_root: Path, run_dir: Path
) -> Path:
    """Create a full patched kernel tree without modifying source or cache checkouts."""
    kernel_source = ensure_git_source(manifest["kernel"], cache_dir, local_root)
    resukisu_source = ensure_git_source(manifest["resukisu"], cache_dir, local_root)
    susfs_source = ensure_git_source(manifest["susfs"], cache_dir, local_root)
    ensure_git_source(manifest["ntsync"], cache_dir, local_root)

    kernel = clone_clean_source(kernel_source, run_dir / "kernel")
    prepare_external_sources(kernel, resukisu_source, susfs_source)
    resukisu_patches = [
        patch for patch in manifest["patches"] if patch.get("target") == "resukisu"
    ]
    unsupported_targets = {
        patch.get("target") for patch in manifest["patches"]
    } - {"kernel", "resukisu"}
    if unsupported_targets:
        raise ValueError(f"unsupported patch targets: {', '.join(sorted(unsupported_targets))}")
    patches_by_id = {patch["id"]: patch for patch in manifest["patches"]}
    integration = patches_by_id["kernel-integration"]
    integration_contents = patch_records([integration])[0]["contents"]
    selected_integration = select_integration_patch(integration_contents)
    apply_patches(kernel, [{"id": "kernel-integration", "contents": selected_integration}])
    for patch_id in ("ntsync-base", "ntsync-linux-4.19-compat"):
        apply_patches(kernel, patch_records([patches_by_id[patch_id]]))
    for patch in resukisu_patches:
        apply_patches(kernel / "KernelSU", patch_records([patch]))
    copy_resukisu_driver(kernel)
    configure_copied_resukisu_driver(kernel)
    disable_copied_resukisu_submodule_guard(kernel)
    assert_source_markers(kernel)
    return kernel


def kernel_build_commands(source: Path, output: Path, compiler: str) -> list[list[str]]:
    """Return the ordered kernel commands without executing them."""
    common = [
        "make",
        "-C",
        str(source),
        "LLVM=1",
        f"CC={compiler}",
        f"O={output}",
        "ARCH=arm64",
    ]
    return [
        [*common, "vendor/kona_defconfig"],
        [*common, "olddefconfig"],
        [*common, "-j4", "Image", "modules"],
    ]


def ccache_environment(base_env: dict[str, str], cache_dir: Path, source: Path) -> dict[str, str]:
    """Return an environment that routes kernel C/C++ compilation through ccache."""
    ccache = shutil.which("ccache")
    if not ccache:
        raise RuntimeError("host ccache is required; install ccache or adjust host PATH")
    resolved_cache = cache_dir.resolve()
    resolved_cache.mkdir(parents=True, exist_ok=True)
    env = base_env.copy()
    env.update(
        {
            "CC": f"{ccache} clang",
            "CXX": f"{ccache} clang++",
            "CCACHE_DIR": str(resolved_cache),
            "CCACHE_BASEDIR": str(source.resolve()),
            "CCACHE_NOHASHDIR": "true",
        }
    )
    return env


def ccache_statistics(cache_dir: Path) -> str:
    """Return host ccache statistics for the script-owned cache directory."""
    ccache = shutil.which("ccache")
    if not ccache:
        raise RuntimeError("host ccache is required; install ccache or adjust host PATH")
    return run([ccache, "--dir", str(cache_dir.resolve()), "--show-stats"]).rstrip()


def sha256(path: Path) -> str:
    """Return the SHA-256 of a file without loading the entire image into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class BootConfigBaseline:
    config: Path
    values: dict[str, str]


def parse_kernel_config(text: str) -> dict[str, str]:
    """Return normalized Kconfig values, including explicitly disabled symbols."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line[2:-11]] = "n"
        elif line.startswith("CONFIG_") and "=" in line:
            name, value = line.split("=", 1)
            values[name] = value
    return values


def write_config_overlay(path: Path, values: dict[str, str]) -> None:
    """Write Kconfig assignments for the fixed full build configuration."""
    lines = []
    for name, value in sorted(values.items()):
        lines.append(f"# {name} is not set" if value == "n" else f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_config_preservation(
    baseline: dict[str, str], final: dict[str, str], allowed_changes: set[str]
) -> None:
    """Reject a build when Kconfig changes outside the full build configuration."""
    changed = {
        name: (baseline.get(name, "n"), final.get(name, "n"))
        for name in baseline.keys() | final.keys()
        if baseline.get(name, "n") != final.get(name, "n")
    }
    unexpected = {name: values for name, values in changed.items() if name not in allowed_changes}
    if unexpected:
        details = ", ".join(
            f"{name}={before}->{after}" for name, (before, after) in sorted(unexpected.items())
        )
        raise RuntimeError(f"kernel configuration changed outside full build configuration: {details}")


def extract_boot_config(input_boot: Path, kernel: Path, run_dir: Path) -> BootConfigBaseline:
    """Extract the original kernel's embedded configuration."""
    magiskboot = shutil.which("magiskboot")
    if not magiskboot:
        raise RuntimeError("host magiskboot is required for boot configuration extraction")
    input_dir = run_dir / "input-boot"
    input_dir.mkdir()
    copied_boot = input_dir / "original-boot.img"
    shutil.copy2(input_boot, copied_boot)
    run([magiskboot, "unpack", "-h", str(copied_boot)], cwd=input_dir, live=True)
    extracted_kernel = input_dir / "kernel"
    if not extracted_kernel.is_file():
        raise RuntimeError("magiskboot did not extract an input boot kernel")
    extractor = kernel / "scripts" / "extract-ikconfig"
    if not extractor.is_file():
        raise RuntimeError(f"missing kernel config extractor: {extractor}")
    config = input_dir / "kernel.config"
    with config.open("w", encoding="utf-8") as stream:
        subprocess.run([str(extractor), str(extracted_kernel)], check=True, stdout=stream)
    values = parse_kernel_config(config.read_text(encoding="utf-8"))
    if not values:
        raise RuntimeError("input boot kernel does not contain an extractable configuration")
    return BootConfigBaseline(config, values)


def render_build_info(
    base_hash: str, patched_hash: str, bbg_status: str, resukisu_version: str
) -> str:
    """Render package provenance that is safe to inspect before flashing."""
    return (
        "Target: Xiaomi Pad 5 Pro Wi-Fi (elish)\n"
        "Package mode: AnyKernel3 direct boot partition flash\n"
        f"Build contents: {FULL_BUILD_DESCRIPTION}\n"
        f"ReSukiSU version code: {resukisu_version}\n"
        f"Base image SHA-256: {base_hash}\n"
        f"Patched SHA-256: {patched_hash}\n"
        "Kernel compiler: Android Clang r563880c\n"
        f"BBG status: {bbg_status}\n"
    )


def build_kernel(
    kernel: Path,
    manifest: dict[str, Any],
    toolchain: Path,
    output: Path,
    ccache_dir: Path,
    boot_config: BootConfigBaseline,
) -> Path:
    """Compile the fixed full overlay against the input boot's exact configuration."""
    toolchain_bin = toolchain / manifest["toolchain"]["bin_subdir"]
    if not (toolchain_bin / "clang").is_file():
        raise RuntimeError(f"missing r563880c clang binary: {toolchain_bin / 'clang'}")
    env = os.environ.copy()
    env["PATH"] = f"{toolchain_bin}:{env['PATH']}"
    env = ccache_environment(env, ccache_dir, kernel)
    commands = kernel_build_commands(kernel, output, env["CC"])
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(boot_config.config, output / ".config")
    overlay = output / "full.config"
    write_config_overlay(overlay, FULL_BUILD_CONFIG)
    run(
        [str(kernel / "scripts/kconfig/merge_config.sh"), "-m", str(output / ".config"), str(overlay)],
        cwd=kernel,
        env={**env, "KCONFIG_CONFIG": str(output / ".config")},
        live=True,
    )
    run(commands[1], env=env, live=True)
    final_config = parse_kernel_config((output / ".config").read_text(encoding="utf-8"))
    expected_values = FULL_BUILD_EXPECTED_CONFIG
    validate_config_preservation(boot_config.values, final_config, set(expected_values))
    for name, value in expected_values.items():
        if final_config.get(name, "n") != value:
            raise RuntimeError(f"full build failed to enable {name}={value}")
    run(commands[2], env=env, live=True)
    image = output / "arch/arm64/boot/Image"
    if not image.is_file():
        raise RuntimeError("kernel build completed without arch/arm64/boot/Image")
    print(ccache_statistics(ccache_dir))
    return image


def repack_boot(input_boot: Path, image: Path, run_dir: Path) -> Path:
    """Replace only the kernel component of a copied boot image using host magiskboot."""
    magiskboot = shutil.which("magiskboot")
    if not magiskboot:
        raise RuntimeError("host magiskboot is required for boot image repacking")
    input_boot = input_boot.resolve()
    image = image.resolve()
    run_dir = run_dir.resolve()
    repack_dir = run_dir / "repack"
    repack_dir.mkdir()
    original = repack_dir / "original-boot.img"
    shutil.copy2(input_boot, original)
    run([magiskboot, "unpack", "-h", str(original)], cwd=repack_dir, live=True)
    shutil.copy2(image, repack_dir / "kernel")
    patched = repack_dir / "elish-patched-boot.img"
    run([magiskboot, "repack", str(original), str(patched)], cwd=repack_dir, live=True)
    header = run([magiskboot, "unpack", "-h", str(patched)], cwd=repack_dir, live=True)
    if "HEADER_VER      [3]" not in header:
        raise RuntimeError("repacked boot image is not Android boot header v3")
    return patched


def create_ak3_package_directory(output_dir: Path) -> Path:
    """Create an isolated package directory without colliding with failed attempts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".ak3-package-", dir=output_dir))


def package_ak3(
    anykernel: Path,
    patched_boot: Path,
    manifest: dict[str, Any],
    output_dir: Path,
    base_hash: str,
    resukisu_version: str,
) -> Path:
    """Create and validate a slot-aware direct-flash AnyKernel3 archive."""
    anykernel = anykernel.resolve()
    patched_boot = patched_boot.resolve()
    output_dir = output_dir.resolve()
    package_dir = create_ak3_package_directory(output_dir)
    shutil.copytree(
        anykernel,
        package_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git"),
    )
    shutil.copy2(patched_boot, package_dir / "boot.img")
    (package_dir / "anykernel.sh").write_text(
        "properties() { '\n"
        f"kernel.string=Elish Infinity-X ReSukiSU {resukisu_version} boot image (clang-r563880c)\n"
        "do.devicecheck=1\n"
        "do.modules=0\n"
        "do.systemless=0\n"
        "do.cleanup=1\n"
        "device.name1=elish\n"
        "'; } # end properties\n\n"
        "BLOCK=boot;\n"
        "IS_SLOT_DEVICE=1;\n"
        ". tools/ak3-core.sh;\n"
        "flash_generic boot;\n",
        encoding="utf-8",
    )
    (package_dir / "AK3_BUILD_INFO.txt").write_text(
        render_build_info(
            base_hash,
            sha256(patched_boot),
            manifest["features"]["bbg"]["status"],
            resukisu_version,
        ),
        encoding="utf-8",
    )
    archive = output_dir / ak3_archive_name(resukisu_version)
    temporary_archive = output_dir / f".{archive.name}.tmp"
    run(
        ["zip", "-r", "-9", "-FS", str(temporary_archive), ".", "-x", ".git/*"],
        cwd=package_dir,
        live=True,
    )
    run(["unzip", "-tq", str(temporary_archive)], live=True)
    run(["sh", "-n", str(package_dir / "anykernel.sh")], live=True)
    temporary_archive.replace(archive)
    shutil.rmtree(package_dir)
    return archive


def resukisu_version_code(resukisu_source: Path) -> str:
    """Calculate ReSukiSU's numeric KSU version from its own Kbuild formula."""
    kbuild = (resukisu_source / "kernel" / "Kbuild").read_text(encoding="utf-8")
    formula = re.search(
        r"^KSU_VERSION\s*:=\s*\$\(shell expr (\d+) \+ \$\(KSU_LOCAL_VERSION\) \+ (\d+)\)",
        kbuild,
        flags=re.MULTILINE,
    )
    if not formula:
        raise RuntimeError("unable to parse ReSukiSU KSU_VERSION formula")
    local_version = run(
        ["git", "-C", str(resukisu_source), "rev-list", "--count", "HEAD"]
    ).strip()
    if not re.fullmatch(r"[0-9]+", local_version):
        raise RuntimeError("unable to read ReSukiSU Git commit count")
    return str(int(formula.group(1)) + int(local_version) + int(formula.group(2)))


def ak3_archive_name(resukisu_version: str) -> str:
    """Return the release archive name for one complete ReSukiSU and SusFS build."""
    if not re.fullmatch(r"[0-9]+", resukisu_version):
        raise ValueError("ReSukiSU version must be numeric")
    return f"elish_Infinity-X_4.19_RESUKI_SUSFS_{resukisu_version}_AK3.zip"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    # Defaults are rooted beside this script so downloads do not depend on cwd.
    parser.add_argument(
        "--manifest", type=Path, default=default_input_path("build_manifest.json", Path("build_manifest.json"))
    )
    parser.add_argument(
        "--boot-img", type=Path, default=default_input_path("boot.img", Path("boot.img"))
    )
    parser.add_argument("--output-dir", type=Path, default=SCRIPT_ROOT / "outputs/ak3")
    parser.add_argument("--cache-dir", type=Path, default=SCRIPT_ROOT / ".cache/ak3-builder")
    parser.add_argument(
        "--ccache-dir", type=Path, default=SCRIPT_ROOT / ".cache/ak3-builder/ccache"
    )
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/ak3"))
    parser.add_argument("--local-root", type=Path, default=SCRIPT_ROOT)
    parser.add_argument("--patches-only", action="store_true")
    parser.add_argument("--keep-workdir", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_manifest(args.manifest)
    manifest = update_resukisu_revision(args.manifest, manifest)
    if not args.boot_img.is_file():
        raise FileNotFoundError(f"input boot image does not exist: {args.boot_img}")

    # Step 1: create an isolated work tree and obtain pinned source revisions.
    print("[phase 1/5] prepare full kernel source and apply patches", flush=True)
    run_dir = create_run_directory(args.work_dir)
    completed = False
    try:
        kernel = prepare_patched_kernel(manifest, args.cache_dir, args.local_root, run_dir)
        print(f"Patched kernel source: {kernel}")
        if args.patches_only:
            print("Patch-only verification completed.")
            completed = True
            return 0

        print("[safety] extract input boot configuration", flush=True)
        boot_config = extract_boot_config(args.boot_img, kernel, run_dir)

        # Step 2: find the fixed r563880c compiler and AnyKernel3 template.
        print("[phase 2/5] acquire compiler and AnyKernel3 sources", flush=True)
        toolchain = ensure_git_source(manifest["toolchain"], args.cache_dir, args.local_root)
        anykernel = ensure_git_source(manifest["anykernel3"], args.cache_dir, args.local_root)

        # Step 3: merge device fragments and compile the kernel image and modules.
        print("[phase 3/5] configure and compile kernel (live output enabled)", flush=True)
        image = build_kernel(kernel, manifest, toolchain, run_dir / "out", args.ccache_dir, boot_config)

        # Step 4: replace only the boot image's kernel payload with magiskboot.
        print("[phase 4/5] repack boot image", flush=True)
        patched_boot = repack_boot(args.boot_img, image, run_dir)

        # Step 5: create and validate the slot-aware AnyKernel3 flash archive.
        print("[phase 5/5] create and verify AnyKernel3 package", flush=True)
        resukisu_version = resukisu_version_code(kernel / "KernelSU")
        archive = package_ak3(
            anykernel,
            patched_boot,
            manifest,
            args.output_dir,
            sha256(args.boot_img),
            resukisu_version,
        )
        print(f"Verified AK3 package: {archive}")
        completed = True
        return 0
    finally:
        if completed and not args.keep_workdir:
            shutil.rmtree(run_dir)
            print(f"Removed temporary work directory: {run_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
