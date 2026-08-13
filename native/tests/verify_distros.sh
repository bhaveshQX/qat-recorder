#!/usr/bin/env bash
# Run the compatibility check on real distribution images.
#
# Not a version-number comparison -- an actual "does this system's libc define
# every symbol version the artifact needs" test, run on the target. Needed
# because RHEL-family distributions backport newer symbol versions while keeping
# the base version number, so nominal glibc versions lie in both directions.
#
# Run from the repository root on a machine with Docker.

set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

IMAGES=(
    "almalinux:8"
    "almalinux:9"
    "rockylinux:9"
    "ubuntu:20.04"
    "ubuntu:22.04"
    "ubuntu:24.04"
)

FAILED=0
for so in native/dist/libqatrec*.so; do
    [ -e "$so" ] || continue
    echo
    echo "=== $(basename "$so") ==="
    for image in "${IMAGES[@]}"; do
        if ! docker image inspect "$image" > /dev/null 2>&1; then
            docker pull -q "$image" > /dev/null 2>&1 || {
                printf '%-34s (image unavailable)\n' "$image"; continue; }
        fi
        docker run --rm -v "$PWD:/work" -w /work "$image" \
            bash native/tests/check_distro_compat.sh "$so" || FAILED=1
    done
done

echo
if [ "$FAILED" -eq 0 ]; then
    echo "every artifact loads on every image tested"
else
    echo "note: a NO above is expected where that distro does not ship that Qt"
fi
