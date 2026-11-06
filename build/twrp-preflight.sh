#!/sbin/sh
# King-iOS TWRP preflight diagnostic — reads ONLY, writes NOTHING.
# Push to /tmp (ramdisk, not USERDATA):
#   adb push twrp-preflight.sh /tmp/
#   adb shell sh /tmp/twrp-preflight.sh
PASS=0; WARN=0; FAIL=0
pass() { echo "  PASS  $1"; PASS=$((PASS+1)); }
warn() { echo "  WARN  $1"; WARN=$((WARN+1)); }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

echo "=== King-iOS TWRP Preflight ==="
echo ""

echo "--- 1. Device identity ---"
MODEL=$(getprop ro.product.model 2>/dev/null)
DEVICE=$(getprop ro.product.device 2>/dev/null)
HARDWARE=$(getprop ro.hardware 2>/dev/null)
echo "  model=$MODEL device=$DEVICE hardware=$HARDWARE"
case "$MODEL" in
  SM-G930F) pass "model is SM-G930F" ;;
  *) fail "expected SM-G930F, got '$MODEL'" ;;
esac
case "$DEVICE" in
  herolte) pass "device is herolte" ;;
  *) fail "expected herolte, got '$DEVICE'" ;;
esac

echo ""
echo "--- 2. BusyBox ---"
BB_VER=$(busybox 2>&1 | head -1)
echo "  $BB_VER"
case "$BB_VER" in
  *BusyBox*) pass "busybox present" ;;
  *) fail "busybox not found" ;;
esac

echo ""
echo "--- 3. Required commands ---"
for cmd in readlink dd od blockdev stat unzip gzip wc head sort tr sed awk grep mount umount sync dirname sha256sum; do
  if busybox $cmd --help >/dev/null 2>&1 || which $cmd >/dev/null 2>&1; then
    pass "$cmd"
  else
    fail "$cmd not available"
  fi
done

echo ""
echo "--- 4. BusyBox behavior tests ---"
# readlink -f
RL=$(readlink -f /proc/self/exe 2>/dev/null)
if [ -n "$RL" ]; then pass "readlink -f works ($RL)"
else fail "readlink -f not functional"; fi

# dd conv=fsync
echo test 2>/dev/null | dd bs=1 count=1 conv=fsync of=/dev/null 2>/dev/null
if [ $? -eq 0 ]; then pass "dd conv=fsync supported"
else fail "dd conv=fsync not supported"; fi

# od -A n -t x1 output format
OD_OUT=$(printf 'AB' | od -A n -t x1 2>/dev/null | tr -d '[:space:]')
case "$OD_OUT" in
  *4142*) pass "od -A n -t x1 works (got $OD_OUT)" ;;
  *) fail "od output unexpected: '$OD_OUT'" ;;
esac

# ash substring expansion
X=abcdef
SUB="${X:0:4}"
if [ "$SUB" = "abcd" ]; then pass "ash substring expansion works"
else fail "substring expansion failed (got '$SUB')"; fi

echo ""
echo "--- 5. BOOT partition ---"
BOOT=/dev/block/platform/155a0000.ufs/by-name/BOOT
if [ -b "$BOOT" ]; then
  pass "BOOT exists: $BOOT"
  BOOT_REAL=$(readlink -f "$BOOT" 2>/dev/null)
  echo "  -> $BOOT_REAL"
  BOOT_SIZE=$(blockdev --getsize64 "$BOOT" 2>/dev/null)
  echo "  size: $BOOT_SIZE bytes"
  if [ "$BOOT_SIZE" = "41943040" ]; then
    pass "BOOT is exactly 41943040 bytes (40 MiB)"
  else
    fail "BOOT size $BOOT_SIZE != expected 41943040"
  fi
else
  fail "BOOT not found: $BOOT"
fi

echo ""
echo "--- 6. USERDATA partition ---"
DATA=/dev/block/platform/155a0000.ufs/by-name/USERDATA
if [ -b "$DATA" ]; then
  pass "USERDATA exists: $DATA"
  DATA_REAL=$(readlink -f "$DATA" 2>/dev/null)
  echo "  -> $DATA_REAL"
  DATA_SIZE=$(blockdev --getsize64 "$DATA" 2>/dev/null)
  DATA_MIB=$((DATA_SIZE / 1048576))
  echo "  size: $DATA_SIZE bytes ($DATA_MIB MiB)"
  if [ "$DATA_MIB" -ge 21000 ] && [ "$DATA_MIB" -le 22000 ]; then
    pass "USERDATA ~21.5 GiB (expected for SM-G930F 32GB)"
  else
    warn "USERDATA $DATA_MIB MiB outside expected 21000-22000 MiB range"
  fi
else
  fail "USERDATA not found: $DATA"
fi

echo ""
echo "--- 7. /tmp space (for BOOT dump) ---"
TMP_FREE=$(df /tmp 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$TMP_FREE" ]; then
  echo "  /tmp available: ${TMP_FREE}K"
  if [ "$TMP_FREE" -gt 50000 ]; then
    pass "/tmp has >50 MiB free (enough for BOOT dump)"
  else
    fail "/tmp has <50 MiB free; BOOT dump may fail"
  fi
else
  warn "cannot read /tmp free space"
fi

echo ""
echo "--- 8. Mount table (storage paths) ---"
mount | grep -E '/sdcard|/data|/external_sd|/usb-otg' 2>&1
DATA_MOUNTED=$(mount | grep ' /data ' 2>/dev/null)
if [ -n "$DATA_MOUNTED" ]; then
  warn "/data is mounted; installer needs USERDATA unmounted for root write"
  echo "  $DATA_MOUNTED"
else
  pass "/data not mounted (good for installation)"
fi

echo ""
echo "--- 9. External storage detection ---"
for path in /external_sd /usb-otg /mnt/media_rw; do
  if [ -d "$path" ]; then
    EXTMNT=$(mount | grep " $path" 2>/dev/null | head -1)
    if [ -n "$EXTMNT" ]; then
      echo "  FOUND: $EXTMNT"
    else
      echo "  dir exists: $path (not mounted)"
    fi
  fi
done

echo ""
echo "========================================="
echo "  Results: $PASS pass, $WARN warn, $FAIL fail"
echo "========================================="
if [ $FAIL -gt 0 ]; then
  echo "  BLOCKERS FOUND — do not install until failures are resolved."
else
  echo "  Device looks ready for King-iOS installation."
fi
