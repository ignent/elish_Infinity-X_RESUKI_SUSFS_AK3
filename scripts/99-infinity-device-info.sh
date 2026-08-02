#!/system/bin/sh

while [ "$(getprop sys.boot_completed)" != "1" ]; do
  sleep 1
done

# Project Infinity X reads only these properties for its Settings device-info page.
resetprop_bin=/data/adb/ksu/bin/resetprop
[ -x "$resetprop_bin" ] || exit 0

"$resetprop_bin" -n ro.infinity.buildtype UNOFFICIAL
"$resetprop_bin" -n ro.infinity.version 3.10
"$resetprop_bin" -n ro.infinity.maintainer "咕咕嘎嘎"
"$resetprop_bin" -n ro.infinity.soc "Qualcomm Snapdragon 870"
"$resetprop_bin" -n ro.infinity.camera "Multi-Lens Module"
"$resetprop_bin" -n ro.infinity.codename elish
"$resetprop_bin" -n ro.infinity.battery "8720 mAh"
"$resetprop_bin" -n ro.infinity.display "1600 x 2560, 120 Hz"
"$resetprop_bin" -n ro.product.marketname "Xiaomi Pad 5 Pro Wi-Fi"