#!/usr/bin/env bash
# Produce a portable qatrec artifact and report exactly what it requires.
#
# Run inside a qatrec-build image (AlmaLinux 8 = RHEL 8 ABI, or 9 for RHEL 9).

set -uo pipefail
cd /work || exit 1

BUILD=/tmp/portable
OUT=/work/native/dist
mkdir -p "$OUT"

echo "=== toolchain ==="
gcc --version | head -1
cmake --version | head -1
echo "glibc: $(ldd --version | head -1)"
echo

echo "=== build (no test app: Widgets not required for the filter) ==="
cmake -S native -B "$BUILD" -DCMAKE_BUILD_TYPE=Release \
      -DQATREC_BUILD_TEST_APP=OFF > /tmp/cfg.log 2>&1
if [ $? -ne 0 ]; then echo "configure FAILED"; tail -25 /tmp/cfg.log; exit 1; fi
cmake --build "$BUILD" -j"$(nproc)" > /tmp/bld.log 2>&1
if [ $? -ne 0 ]; then echo "build FAILED"; tail -30 /tmp/bld.log; exit 1; fi

LIB=$(find "$BUILD" -name 'libqatrec*.so' | head -1)
[ -f "$LIB" ] || { echo "no artifact produced"; exit 1; }
cp "$LIB" "$OUT/"
echo "artifact: $(basename "$LIB")  ($(stat -c%s "$LIB") bytes)"
echo

echo "=== what it requires ==="
bash "$(dirname "$0")/check_symbol_floor.sh" "$LIB"
echo
echo "  shared dependencies:"
ldd "$LIB" 2>/dev/null | awk '{print "    " $1}' | sort | head -15
echo
echo "  NOTE: comparing this floor against a distribution's nominal glibc"
echo "  version is not a reliable compatibility test -- RHEL-family distributions"
echo "  backport newer symbol versions while keeping the base number, and they do"
echo "  not all backport the same set (AlmaLinux 9.8 defines up to GLIBC_2.35,"
echo "  Rocky 9.3 only 2.34). Run native/tests/verify_distros.sh, which checks"
echo "  each target's real libc."
