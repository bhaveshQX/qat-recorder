#!/usr/bin/env bash
# The whole verification suite, as one script.
#
# CI systems come and go; this script is the actual definition of "green", and
# the GitLab/GitHub files are thin wrappers that call it. It is also what you run
# locally before pushing, so there is one thing to keep working instead of three.
#
# Stages, cheapest first, so a failure surfaces as early as possible:
#
#   1  offline tests            no display, no Qt, no application     ~3s
#   2  packaging                the wheel builds and imports          ~10s
#   3  native build + filter    real X server, real input             ~60s
#   4  record -> emit -> replay the loop that matters                 ~60s
#   5  portable artifacts       one per Qt major, oldest-glibc build  ~60s
#
# Usage:  bash ci/run.sh [stage ...]     (default: all)

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"

STAGES=("$@")
if [ ${#STAGES[@]} -eq 0 ]; then
    STAGES=(offline package native replay portable)
fi

FAILED=()
run_stage() {
    local name="$1"; shift
    printf '\n\033[1m=== %s ===\033[0m\n' "$name"
    if "$@"; then
        echo "--- $name: OK"
    else
        echo "--- $name: FAILED"
        FAILED+=("$name")
    fi
}

wants() {
    local wanted="$1"
    for stage in "${STAGES[@]}"; do
        [ "$stage" = "$wanted" ] && return 0
    done
    return 1
}

# ---------------------------------------------------------------------------

stage_offline() {
    local py="${PYTHON:-python3}"
    "$py" -m pip install --quiet pytest || return 1
    # PySide6 is optional: without it the panel tests skip rather than fail, so
    # the core suite stays runnable on a machine with no Qt bindings at all.
    "$py" -m pip install --quiet PySide6-Essentials 2>/dev/null || \
        echo "note: PySide6 unavailable, panel tests will skip"
    QT_QPA_PLATFORM=offscreen PYTHONPATH="$ROOT" "$py" -m pytest tests -q
}

stage_package() {
    local py="${PYTHON:-python3}"
    "$py" -m pip install --quiet build || return 1
    rm -rf dist
    "$py" -m build --wheel --outdir dist || return 1
    local wheel
    wheel=$(ls dist/*.whl 2>/dev/null | head -1)
    [ -n "$wheel" ] || { echo "no wheel produced"; return 1; }
    echo "built $wheel"

    # Confirm the core imports with no dependencies at all -- no qat, no Qt.
    #
    # Installed with --target rather than into a venv: some distributions ship
    # python3 without ensurepip, and a packaging check that needs root to run is
    # not much of a check. The import runs from /tmp so that the repository's own
    # source tree cannot satisfy it -- cwd precedes PYTHONPATH on sys.path, so
    # running it from here would test the sources and pass even for a broken
    # wheel.
    rm -rf /tmp/wheelcheck
    "$py" -m pip install --quiet --target /tmp/wheelcheck "$wheel" --no-deps || return 1
    ( cd /tmp && PYTHONPATH=/tmp/wheelcheck "$py" -c "
import qat_recorder, os
assert '/tmp/wheelcheck' in os.path.dirname(qat_recorder.__file__), qat_recorder.__file__
from qat_recorder.audit import audit
from qat_recorder.emit import emit_python, emit_gherkin
from qat_recorder.capture import CaptureSession
from qat_recorder.native import source_dir
assert (source_dir() / 'qatrec.cpp').exists(), 'C++ source missing from the wheel'
print('wheel imports cleanly:', qat_recorder.__version__)
" ) || return 1
}

stage_native() {
    docker build -q -f docker/Dockerfile.test -t qatrec-test . || return 1
    docker run --rm -v "$ROOT:/work" qatrec-test bash native/tests/run_filter_test.sh
}

stage_replay() {
    docker build -q -f docker/Dockerfile.test -t qatrec-test . || return 1
    docker run --rm -v "$ROOT:/work" qatrec-test bash native/tests/run_replay.sh
}

stage_portable() {
    local ok=0
    # Qt 5.15 on EL8: the oldest target, and the only Qt that distro ships.
    docker build -q -f docker/Dockerfile.build \
        --build-arg BASE=almalinux:8 --build-arg QT_PKG=qt5-qtbase-devel \
        -t qatrec-build-el8 . || ok=1
    docker run --rm -v "$ROOT:/work" qatrec-build-el8 \
        bash native/tests/build_portable.sh || ok=1

    # Qt 6 on EL9. Qt 6 is not in RHEL 9 AppStream; EPEL supplies 6.6.
    docker build -q -f docker/Dockerfile.build \
        --build-arg BASE=almalinux:9 --build-arg QT_PKG=qt6-qtbase-devel \
        -t qatrec-build-el9 . || ok=1
    docker run --rm -v "$ROOT:/work" qatrec-build-el9 \
        bash native/tests/build_portable.sh || ok=1
    return "$ok"
}

# ---------------------------------------------------------------------------

wants offline  && run_stage "1. offline tests"        stage_offline
wants package  && run_stage "2. packaging"            stage_package
wants native   && run_stage "3. native filter"        stage_native
wants replay   && run_stage "4. record/emit/replay"   stage_replay
wants portable && run_stage "5. portable artifacts"   stage_portable

printf '\n\033[1m=== summary ===\033[0m\n'
if [ ${#FAILED[@]} -eq 0 ]; then
    echo "all stages passed"
    exit 0
fi
printf 'failed: %s\n' "${FAILED[*]}"
exit 1
