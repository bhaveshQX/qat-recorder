#!/usr/bin/env bash
# The loop that actually matters: record a session, generate a test from it,
# then RUN that test against the application and see it pass.
#
# Compiling proves the generated code is syntactically valid; running proves the
# recorder understood what happened. Running it is what found three real bugs in
# shortcut handling that compiling never would have.
#
# Note that replay does NOT preload the event filter. Recording needs it;
# replaying is pure Qat. The application is registered directly.

set -uo pipefail
cd /work || exit 1

BUILD=/tmp/build_replay
GEN=/tmp/generated
APP_NAME=qatrec_e2e

echo "=== 1. build ==="
cmake -S native -B "$BUILD" -DCMAKE_BUILD_TYPE=Release > /tmp/r_cmake.log 2>&1 \
    && cmake --build "$BUILD" -j"$(nproc)" > /tmp/r_build.log 2>&1
if [ $? -ne 0 ]; then echo "build FAILED"; tail -20 /tmp/r_build.log; exit 1; fi

LIB=$(find "$BUILD" -name 'libqatrec*.so' | head -1)
APP="$BUILD/qatrec_test_app"
echo "library: $LIB"

echo
echo "=== 2. X server ==="
Xvfb :99 -screen 0 1024x768x24 > /tmp/r_xvfb.log 2>&1 &
XVFB=$!
sleep 2
export DISPLAY=:99

echo
echo "=== 3. record ==="
export QATREC_LIB="$LIB" QATREC_APP="$APP"
PYTHONPATH=/work python3 native/tests/e2e_record.py > /tmp/r_record.log 2>&1
RC=$?
grep -E "captured|folded|redacted|ok:" /tmp/r_record.log
if [ "$RC" -ne 0 ]; then
    echo "recording FAILED"; tail -25 /tmp/r_record.log; kill "$XVFB"; exit 1
fi

echo
echo "=== 4. generate ==="
PYTHONPATH=/work python3 -m qat_recorder emit spike/e2e_recording.json --out "$GEN" || {
    kill "$XVFB"; exit 1; }
echo "steps in the generated test:"
grep -E "^    qat\." "$GEN/test_recorded.py" | sed 's/^/  /'
echo "object definitions:"
grep -E "^[A-Z_0-9]+ = " "$GEN/test_recorded.py" | sed 's/^/  /'

echo
echo "=== 5. register the application for replay (no filter) ==="
PYTHONPATH=/work python3 - <<PY
import qat
qat.test_settings.Settings.lock_ui = "never"
qat.register_application("${APP_NAME}", "${APP}")
print("registered ${APP_NAME} ->", qat.get_application_path("${APP_NAME}"))
PY

echo
echo "=== 6. REPLAY the generated test ==="
cp "$GEN/test_recorded.py" /tmp/test_generated_replay.py
QATREC_SECRET_passwordField="hunter2" PYTHONPATH=/work \
    python3 -m pytest /tmp/test_generated_replay.py -v --no-header -p no:cacheprovider \
    > /tmp/r_pytest.log 2>&1
REPLAY_RC=$?
echo "--- failure detail ---"
grep -nE "^E |RuntimeError|AssertionError|LookupError|TypeError" /tmp/r_pytest.log | head -15
echo "--- last lines ---"
tail -6 /tmp/r_pytest.log

PYTHONPATH=/work python3 -c "import qat; qat.unregister_application('${APP_NAME}')" 2>/dev/null
kill "$XVFB" 2>/dev/null

echo
if [ "$REPLAY_RC" -eq 0 ]; then
    echo "REPLAY PASSED — a recorded session was regenerated and re-executed"
else
    echo "REPLAY FAILED (rc=$REPLAY_RC)"
fi
exit "$REPLAY_RC"
