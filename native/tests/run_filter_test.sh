#!/usr/bin/env bash
# Build the qatrec filter and prove the two behaviours that justify it:
#
#   A. real user input arriving through the window system IS captured
#   B. programmatic input inside the process is NOT captured
#
# Run inside the qatrec-test image, with the repo mounted at /work.

set -uo pipefail
cd /work || exit 1

PORT_A=45551
PORT_B=45552
BUILD=/tmp/build
FAILED=0

hr() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; FAILED=1; }
pass() { echo "ok: $1"; }

hr "1. Build"
cmake -S native -B "$BUILD" -DCMAKE_BUILD_TYPE=Release > /tmp/cmake.log 2>&1
if [ $? -ne 0 ]; then echo "cmake configure FAILED"; tail -30 /tmp/cmake.log; exit 1; fi
cmake --build "$BUILD" -j"$(nproc)" > /tmp/build.log 2>&1
if [ $? -ne 0 ]; then echo "build FAILED"; tail -40 /tmp/build.log; exit 1; fi

LIB=$(find "$BUILD" -name 'libqatrec*.so' | head -1)
APP="$BUILD/qatrec_test_app"
echo "library : $LIB"
echo "app     : $APP"
[ -f "$LIB" ] || { echo "no library produced"; exit 1; }
[ -x "$APP" ] || { echo "no test app produced"; exit 1; }

echo
echo "-- what the artifact requires --"
bash native/tests/check_symbol_floor.sh "$LIB" 2>/dev/null | sed 's/^/  /'

hr "2. Start X server"
Xvfb :99 -screen 0 1024x768x24 > /tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 2
export DISPLAY=:99
xdpyinfo > /dev/null 2>&1 && pass "Xvfb running" || { fail "Xvfb did not start"; exit 1; }

# Assert on parsed JSON, not on text. The listener re-serialises records with
# json.dumps, so greping for exact byte patterns tests the formatter, not the
# filter -- which is how the first version of this script produced two false
# failures against a perfectly good capture.
check_json() {
    python3 - "$1" "$2" <<'PY'
import json, sys
path, check = sys.argv[1], sys.argv[2]
records = [json.loads(line) for line in open(path) if line.strip()]
kinds = {r.get("kind") for r in records}
if check == "keyboard":
    ok = "key_press" in kinds
elif check == "mouse":
    ok = "mouse_press" in kinds and "mouse_release" in kinds
elif check == "locator":
    ok = all(r.get("target", {}).get("class") for r in records)
elif check == "named":
    ok = any(r["target"].get("objectName") for r in records)
elif check == "path":
    ok = any(r["target"].get("path") for r in records)
elif check == "no_text":
    ok = not any("text" in r for r in records)
else:
    ok = False
sys.exit(0 if ok else 1)
PY
}

# -------------------------------------------------------------------------
hr "3. Case A — REAL user input (xdotool drives the X server)"
python3 native/tests/listener.py "$PORT_A" /tmp/events_a.jsonl 25 > /tmp/listener_a.log 2>&1 &
sleep 1

LD_PRELOAD="$LIB" QATREC_PORT="$PORT_A" "$APP" --quit-after 20000 \
    > /tmp/app_a.log 2>&1 &
APP_PID=$!
sleep 4

WID=$(xdotool search --name "QatRecTestApp" 2>/dev/null | head -1)
if [ -z "$WID" ]; then WID=$(xdotool search --class "qatrec_test_app" 2>/dev/null | head -1); fi
echo "window id: ${WID:-<not found>}"

if [ -n "$WID" ]; then
    xdotool windowactivate "$WID" 2>/dev/null
    sleep 1
    xdotool mousemove --window "$WID" 60 60 click 1
    sleep 0.4
    xdotool mousemove --window "$WID" 80 200 click 1
    sleep 0.4
    xdotool key Tab
    sleep 0.2
    xdotool type --delay 60 "hello"
    sleep 1.5
else
    fail "could not find the application window"
fi

kill "$APP_PID" 2>/dev/null
wait "$APP_PID" 2>/dev/null
sleep 1
cat /tmp/listener_a.log

COUNT_A=$(wc -l < /tmp/events_a.jsonl 2>/dev/null || echo 0)
echo "records captured: $COUNT_A"
if [ "$COUNT_A" -gt 0 ]; then pass "real input was captured"; else fail "no real input captured"; fi

for check in keyboard mouse locator named path; do
    if check_json /tmp/events_a.jsonl "$check"; then
        pass "$check"
    else
        fail "$check"
    fi
done

echo
echo "-- sample records --"
head -3 /tmp/events_a.jsonl 2>/dev/null

echo
echo "-- typed characters must NOT reach the stream (passwords) --"
if check_json /tmp/events_a.jsonl no_text; then
    pass "no typed characters in the stream"
else
    fail "typed characters leaked into the stream"
fi

# -------------------------------------------------------------------------
hr "4. Case B — PROGRAMMATIC input only (no xdotool at all)"
python3 native/tests/listener.py "$PORT_B" /tmp/events_b.jsonl 18 > /tmp/listener_b.log 2>&1 &
sleep 1

LD_PRELOAD="$LIB" QATREC_PORT="$PORT_B" "$APP" --programmatic --quit-after 9000 \
    > /tmp/app_b.log 2>&1 &
APP_PID_B=$!
wait "$APP_PID_B" 2>/dev/null
sleep 2
cat /tmp/listener_b.log

COUNT_B=$(wc -l < /tmp/events_b.jsonl 2>/dev/null || echo 0)
echo "records captured: $COUNT_B"
if [ "$COUNT_B" -eq 0 ]; then
    pass "programmatic input correctly ignored (spontaneous() works)"
else
    fail "programmatic input was recorded ($COUNT_B records) - spontaneous() gate is broken"
    head -3 /tmp/events_b.jsonl
fi

# -------------------------------------------------------------------------
hr "5. Inert without QATREC_PORT"
"$APP" --quit-after 3000 > /tmp/app_c.log 2>&1
if grep -q "qatrec:" /tmp/app_c.log; then
    fail "filter activated without QATREC_PORT"
else
    pass "library is inert when not asked to record"
fi

kill "$XVFB_PID" 2>/dev/null

hr "Result"
if [ "$FAILED" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED"; fi
exit "$FAILED"
