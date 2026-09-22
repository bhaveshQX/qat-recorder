#!/bin/sh
# Three launches of the same application, 25 seconds each, and what differs.
#
# "It starts by hand and not from the recorder" is a statement about the
# environment, and there are only so many things in it. This runs the
# application exactly as a person does, then adds the recorder's environment as
# it currently is, then adds it with one thing removed -- the injector in the
# shell that runs the script.
#
#     plain            what works today
#     as-it-is-now     what the recorder does today
#     with-the-fix     the same, minus the injector in the launching shell
#
# If `as-it-is-now` fails and `with-the-fix` works, the diagnosis is right and
# the fix is the fix. If both fail, it is wrong and the answer is somewhere in
# the logs this leaves in /tmp/qatrec-diagnose.
#
#     sh diagnose-launch.sh /path/to/start_spine.sh
#
# Run it from a normal terminal, not from the agent.

set -u

APP="${1:-}"
if [ -z "${APP}" ] || [ ! -f "${APP}" ]; then
    echo "usage: sh diagnose-launch.sh /path/to/start_app.sh" >&2
    exit 2
fi

case "${APP}" in
    /*) ;;
    *) APP="$(cd "$(dirname "${APP}")" && pwd)/$(basename "${APP}")" ;;
esac
DIR="${APP%/*}"

GATE="${QATREC_GATE:-$HOME/qatrec-filter/libqatgate.so}"
INJECTOR="$(ls "$HOME"/qatrec/lib/python*/site-packages/qat/bin/libinjector.so \
    2>/dev/null | head -1)"

OUT=/tmp/qatrec-diagnose
mkdir -p "${OUT}"

echo "application : ${APP}"
echo "gate        : ${GATE}$([ -f "${GATE}" ] || echo '   (MISSING)')"
echo "injector    : ${INJECTOR:-(not found)}"
echo "logs        : ${OUT}"
echo

if [ ! -f "${GATE}" ] || [ -z "${INJECTOR}" ]; then
    echo "Cannot run the comparison without both. Build the filter first:" >&2
    echo "  ~/qatrec/bin/python -m qat_recorder build-filter --out ~/qatrec-filter" >&2
    exit 2
fi

# Everything this application starts, stopped between runs, so each one begins
# from the same place.
stop_everything() {
    pkill -x spine 2>/dev/null
    pkill -f 'storescp_' 2>/dev/null
    sleep 2
}

report() {
    name="$1"
    log="${OUT}/${name}.log"
    printf '  injector output in the script: %s\n' \
        "$(grep -c 'OnUnload\|Loading injector' "${log}" 2>/dev/null || echo 0)"
    printf '  paths that did not resolve   : %s\n' \
        "$(grep -c 'not found\|No such file' "${log}" 2>/dev/null || echo 0)"
    printf '  a path with a word wedged in : %s\n' \
        "$(grep -c 'OnUnload/' "${log}" 2>/dev/null || echo 0)"
    echo "  full output: ${log}"
    echo
}

echo "=== 1. plain -- the way it works today ==="
echo "    (watch for the Installation Error dialog)"
stop_everything
( cd "${DIR}" && timeout 25 "${APP}" ) > "${OUT}/plain.log" 2>&1
stop_everything
report plain

echo "=== 2. as-it-is-now -- the recorder's environment ==="
echo "    (QATREC_PID is set, so the injector loads in the shell)"
stop_everything
( cd "${DIR}" \
  && QT_QPA_PLATFORMTHEME= LD_PRELOAD="${GATE}" QATREC_PRELOAD="${INJECTOR}" \
     timeout 25 sh -c 'QATREC_PID=$$; export QATREC_PID; exec "$0"' "${APP}" \
) > "${OUT}/as-it-is-now.log" 2>&1
stop_everything
report as-it-is-now

echo "=== 3. with-the-fix -- the same, minus that one thing ==="
stop_everything
( cd "${DIR}" \
  && QT_QPA_PLATFORMTHEME= LD_PRELOAD="${GATE}" QATREC_PRELOAD="${INJECTOR}" \
     timeout 25 "${APP}" \
) > "${OUT}/with-the-fix.log" 2>&1
stop_everything
report with-the-fix

echo "What it means:"
echo "  2 fails and 3 works  -> the diagnosis is right, and the fix is the fix."
echo "  2 and 3 both fail    -> it is wrong. Send ${OUT}/*.log and we look again."
echo "  1 fails too          -> the application is unhappy for its own reasons."
