# Elish Infinity X ReSukiSU Kernel

This repository builds a ReSukiSU-enabled custom kernel for the Xiaomi Pad 5
Pro Wi-Fi (`elish`). It is based on Infinity X from
[xiliahz/device_xiao](https://github.com/xiliahz/device_xiao).

The build integrates ReSukiSU, SusFS, NTSync, BBR, and DroidSpaces-related
networking and namespace configuration. It packages the resulting boot image
as a slot-aware AnyKernel3 ZIP.

## Build

Ensure that `build_ak3.py` and `boot.img` are in the same directory.
Run the full build from the repository root:

```sh
python3 build_ak3.py
```

The script updates ReSukiSU before building and writes the package to:

```text
outputs/ak3/elish_Infinity-X_4.19_RESUKI_SUSFS_<resukisu-version>_AK3.zip
```
