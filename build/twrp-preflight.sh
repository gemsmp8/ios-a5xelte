#!/sbin/sh
# King-iOS TWRP preflight diagnostic — reads ONLY, writes NOTHING.
# Run: adb shell sh /sdcard/twrp-preflight.sh
# Or push to /tmp: adb push twrp-preflight.sh /tmp/ && adb shell sh /tmp/twrp-preflight.sh

echo "=== King-iOS TWRP Preflight ==="
echo ""

echo "--- Device ---"
getprop ro.product.model
getprop ro.hardware

echo ""
echo "--- BusyBox ---"
busybox --help 2>&1 | head -1

echo ""
echo "--- Critical commands ---"
OK=0; FAIL=0
for cmd in readlink dd od blockdev stat unzip gzip wc head sort tr sed awk grep mount umount sync dirname sha256sum; do
  if busybox $cmd --help >/dev/null 2>&1 || which $cmd >/dev/null 2>&1; then
    echo "  OK   $cmd"
    OK=$((OK+1))
  else
    echo "  FAIL $cmd"
    FAIL=$((FAIL+1))
  fi
done
echo "  $OK ok, $FAIL missing"

echo ""
echo "--- readlink -f test ---"
readlink -f /sdcard 2>&1 || echo "FAIL: readlink -f not available"

echo ""
echo "--- dd conv=fsync test ---"
echo test | dd bs=1 count=1 conv=fsync of=/dev/null 2>&1
echo "exit code: $?"

echo ""
echo "--- od format test ---"
echo AB | od -A n -t x1 2>&1

echo ""
echo "--- ash substring expansion test ---"
X=abcdef; echo "${X:0:4}" 2>&1

echo ""
echo "--- BOOT partition ---"
BOOT=/dev/block/platform/155a0000.ufs/by-name/BOOT
if [ -b "$BOOT" ]; then
  echo "exists: $BOOT"
  blockdev --getsize64 "$BOOT" 2>&1
else
  echo "NOT FOUND: $BOOT"
fi

echo ""
echo "--- USERDATA partition ---"
DATA=/dev/block/platform/155a0000.ufs/by-name/USERDATA
if [ -b "$DATA" ]; then
  echo "exists: $DATA"
  blockdev --getsize64 "$DATA" 2>&1
else
  echo "NOT FOUND: $DATA"
fi

echo ""
echo "--- Mount table (sdcard/data lines) ---"
mount | grep -E '/sdcard|/data' 2>&1

echo ""
echo "=== Preflight complete ==="
