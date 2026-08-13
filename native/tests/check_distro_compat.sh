#!/usr/bin/env bash
# Does THIS distribution's libc define the symbol versions an artifact needs?
#
# Run inside a target distribution's container with the artifact mounted. This
# is the only sound compatibility test: comparing against a distribution's
# nominal glibc version is wrong in both directions, because RHEL-family
# distributions backport newer symbol versions while keeping the base number.
#
# Uses `grep -a` rather than `strings`: minimal Debian and Ubuntu images do not
# ship binutils. An earlier version used `strings`, found nothing on those
# images, compared two empty sets, and cheerfully reported LOADS for artifacts
# that could not possibly have loaded.

set -uo pipefail
SO="${1:?usage: check_distro_compat.sh <library>}"

versions() {   # every GLIBC_x.y token in a binary, deduplicated
    grep -ao 'GLIBC_[0-9][0-9.]*' "$1" 2>/dev/null | sort -V -u
}

NAME=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")
NOMINAL=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')

LIBC=$(ldconfig -p 2>/dev/null | awk '/libc\.so\.6 /{print $NF; exit}')
for candidate in "$LIBC" /lib/x86_64-linux-gnu/libc.so.6 /lib64/libc.so.6 \
                 /usr/lib/x86_64-linux-gnu/libc.so.6 /usr/lib64/libc.so.6; do
    if [ -n "$candidate" ] && [ -e "$candidate" ]; then LIBC="$candidate"; break; fi
done

if [ ! -e "${LIBC:-}" ]; then
    printf '%-34s CANNOT VERIFY (libc.so.6 not found)\n' "${NAME:-unknown}"
    exit 2
fi

DEFINED=$(versions "$LIBC")
NEEDED=$(versions "$SO")

# Refuse to answer rather than answer wrongly.
if [ -z "$NEEDED" ] || [ -z "$DEFINED" ]; then
    printf '%-34s CANNOT VERIFY (no version symbols readable)\n' "${NAME:-unknown}"
    exit 2
fi

HIGHEST=$(echo "$DEFINED" | tail -1)
MISSING=""
for want in $NEEDED; do
    echo "$DEFINED" | grep -qx "$want" || MISSING="$MISSING $want"
done

printf '%-34s nominal %-7s defines up to %-14s ' \
    "${NAME:-unknown}" "${NOMINAL:-?}" "$HIGHEST"
if [ -z "$MISSING" ]; then
    echo "LOADS"
    exit 0
fi
echo "NO (missing:$MISSING )"
exit 1
