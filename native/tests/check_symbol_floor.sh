#!/usr/bin/env bash
# Measure an artifact's real symbol-version floor, and say which symbols set it.
#
# Two lessons are baked in here.
#
# 1. `strings | grep GLIBC_` is a heuristic. readelf's version-requirement
#    section is the authority.
# 2. Comparing against a distribution's *nominal* glibc version is unreliable.
#    RHEL-family distributions backport newer symbol versions while keeping the
#    base version number, so a binary built on AlmaLinux 9 -- nominally glibc
#    2.34 -- can legitimately require GLIBC_2.35. The only sound test is whether
#    the target's libc actually DEFINES the versions the artifact needs, which
#    check_distro_compat.sh does by running on the target.

set -uo pipefail
SO="${1:?usage: check_symbol_floor.sh <library>}"

echo "=== $(basename "$SO") ==="
echo
echo "-- required from libc, per readelf (authoritative) --"
NEEDED=$(readelf --version-info "$SO" 2>/dev/null \
    | awk '/File: libc.so.6/{inlibc=1; next} /File: /{inlibc=0} inlibc' \
    | grep -o 'GLIBC_[0-9.]*' | sort -V -u)
echo "$NEEDED" | sed 's/^/  /'
FLOOR=$(echo "$NEEDED" | tail -1)
echo
echo "  floor: ${FLOOR:-none}"
echo
echo "-- symbols responsible for the floor --"
objdump -T "$SO" 2>/dev/null | grep -F "$FLOOR" \
    | awk '{print "  " $NF}' | sort -u | head -20
echo
echo "-- libstdc++ --"
CXX=$(readelf --version-info "$SO" 2>/dev/null \
    | awk '/File: libstdc\+\+/{inc=1; next} /File: /{inc=0} inc' \
    | grep -o 'GLIBCXX_[0-9.]*' | sort -V -u | tail -1)
echo "  ${CXX:-none (statically linked)}"
