#!/usr/bin/env bash
#
# One command per job. Detects the distribution and does the rest.
#
#   bash install.sh vm          everything needed to record on THIS machine
#   bash install.sh vm --agent  ...plus the agent, so testers can drive it remotely
#   bash install.sh local       a tester's machine: the panel only
#   bash install.sh uninstall   remove all of it
#   bash install.sh doctor      what is installed, what is missing, what is wrong
#
# Options
#   --wheel FILE   path to qat_recorder-*.whl  (default: found next to this script)
#   --qt 5|6       which Qt to build the filter against (default: auto-detect)
#   --app PATH     the application you intend to record; used to detect Qt
#   --prefix DIR   install location (default: ~/qatrec)
#   --yes          do not prompt

set -uo pipefail

PREFIX="${QATREC_PREFIX:-$HOME/qatrec}"
FILTER_DIR="$PREFIX-filter"
WHEEL=""
QT_MAJOR=""
APP=""
ASSUME_YES=0
WITH_AGENT=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step()  { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$OFF"; }
ok()    { printf '    %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
warn()  { printf '    %s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
fail()  { printf '\n%serror:%s %s\n' "$RED" "$OFF" "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Distribution detection
# ---------------------------------------------------------------------------

detect_os() {
    OS_ID=""; OS_LIKE=""; OS_NAME="unknown"; PKG=""
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_ID="${ID:-}"; OS_LIKE="${ID_LIKE:-}"; OS_NAME="${PRETTY_NAME:-$ID}"
    fi
    case " $OS_ID $OS_LIKE " in
        *" rhel "*|*" fedora "*|*" centos "*)  PKG=dnf ;;
        *" debian "*|*" ubuntu "*)             PKG=apt ;;
        *" suse "*|*" opensuse "*)             PKG=zypper ;;
        *" arch "*)                            PKG=pacman ;;
        *)
            command -v dnf     >/dev/null && PKG=dnf
            [ -z "$PKG" ] && command -v apt-get >/dev/null && PKG=apt
            [ -z "$PKG" ] && command -v zypper  >/dev/null && PKG=zypper
            [ -z "$PKG" ] && command -v pacman  >/dev/null && PKG=pacman
            ;;
    esac
    [ -n "$PKG" ] || fail "could not identify the package manager on $OS_NAME"
}

SUDO=""
need_sudo() {
    if [ "$(id -u)" -eq 0 ]; then SUDO=""; return 0; fi
    command -v sudo >/dev/null || fail "system packages are needed but sudo is not available; re-run as root"
    SUDO="sudo"
}

# Map a role to this distribution's package name.
pkg_for() {
    case "$PKG:$1" in
        dnf:compiler)    echo "gcc-c++" ;;
        apt:compiler)    echo "g++" ;;
        zypper:compiler) echo "gcc-c++" ;;
        pacman:compiler) echo "gcc" ;;

        *:cmake)         echo "cmake" ;;

        dnf:qt5)         echo "qt5-qtbase-devel" ;;
        apt:qt5)         echo "qtbase5-dev" ;;
        zypper:qt5)      echo "libqt5-qtbase-devel" ;;
        pacman:qt5)      echo "qt5-base" ;;

        dnf:qt6)         echo "qt6-qtbase-devel" ;;
        apt:qt6)         echo "qt6-base-dev" ;;
        zypper:qt6)      echo "qt6-base-devel" ;;
        pacman:qt6)      echo "qt6-base" ;;

        dnf:qml5)        echo "qt5-qtdeclarative-devel" ;;
        apt:qml5)        echo "qtdeclarative5-dev" ;;
        zypper:qml5)     echo "libqt5-qtdeclarative-devel" ;;
        pacman:qml5)     echo "qt5-declarative" ;;

        dnf:qml6)        echo "qt6-qtdeclarative-devel" ;;
        apt:qml6)        echo "qt6-declarative-dev" ;;
        zypper:qml6)     echo "qt6-declarative-devel" ;;
        pacman:qml6)     echo "qt6-declarative" ;;

        # Only needed to build a filter that runs on OTHER machines; the build
        # falls back to dynamic linking without it.
        dnf:staticcxx)   echo "libstdc++-static" ;;
        *:staticcxx)     echo "" ;;

        apt:venv)        echo "python3-venv" ;;
        *:venv)          echo "" ;;

        *) echo "" ;;
    esac
}

pkg_install() {
    local packages=("$@")
    [ ${#packages[@]} -gt 0 ] || return 0
    need_sudo
    case "$PKG" in
        dnf)    $SUDO dnf install -y "${packages[@]}" ;;
        apt)    $SUDO apt-get update -qq && $SUDO apt-get install -y "${packages[@]}" ;;
        zypper) $SUDO zypper --non-interactive install "${packages[@]}" ;;
        pacman) $SUDO pacman -S --noconfirm --needed "${packages[@]}" ;;
    esac
}

# ---------------------------------------------------------------------------
# Qt detection
# ---------------------------------------------------------------------------

detect_qt() {
    if [ -n "$QT_MAJOR" ]; then ok "Qt $QT_MAJOR (specified)"; return; fi

    # Best signal: what the application you intend to record actually links.
    if [ -n "$APP" ] && [ -e "$APP" ]; then
        if ldd "$APP" 2>/dev/null | grep -q libQt6Core; then
            QT_MAJOR=6; ok "Qt 6 (from $APP)"; return
        elif ldd "$APP" 2>/dev/null | grep -q libQt5Core; then
            QT_MAJOR=5; ok "Qt 5 (from $APP)"; return
        fi
    fi

    # Otherwise: whichever Qt runtime is installed.
    if ldconfig -p 2>/dev/null | grep -q libQt6Core; then
        QT_MAJOR=6; ok "Qt 6 (found on this system)"
    elif ldconfig -p 2>/dev/null | grep -q libQt5Core; then
        QT_MAJOR=5; ok "Qt 5 (found on this system)"
    else
        QT_MAJOR=6
        warn "no Qt runtime detected; defaulting to Qt 6 (override with --qt 5)"
    fi
}

# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

find_wheel() {
    [ -n "$WHEEL" ] && { [ -e "$WHEEL" ] || fail "no such wheel: $WHEEL"; return; }
    for candidate in "$SCRIPT_DIR"/qat_recorder-*.whl \
                     "$SCRIPT_DIR"/dist/qat_recorder-*.whl \
                     ./qat_recorder-*.whl ./dist/qat_recorder-*.whl \
                     "$HOME"/qat_recorder-*.whl "$HOME"/Downloads/qat_recorder-*.whl; do
        [ -e "$candidate" ] && { WHEEL="$candidate"; return; }
    done
    fail "could not find qat_recorder-*.whl — pass it with --wheel FILE"
}

make_venv() {
    if [ ! -x "$PREFIX/bin/python" ]; then
        python3 -m venv "$PREFIX" 2>/dev/null || {
            warn "python3-venv is missing; installing it"
            local venv_pkg; venv_pkg=$(pkg_for venv)
            [ -n "$venv_pkg" ] && pkg_install "$venv_pkg"
            python3 -m venv "$PREFIX" || fail "could not create a virtual environment at $PREFIX"
        }
    fi
    "$PREFIX/bin/python" -m pip install --quiet --upgrade pip
    ok "environment at $PREFIX"
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_vm() {
    step "Machine"
    detect_os
    ok "$OS_NAME (using $PKG)"
    find_wheel
    ok "wheel: $WHEEL"
    detect_qt

    step "System packages"
    local wanted=()
    wanted+=("$(pkg_for compiler)")
    wanted+=("$(pkg_for cmake)")
    wanted+=("$(pkg_for "qt$QT_MAJOR")")
    local extra
    extra=$(pkg_for staticcxx); [ -n "$extra" ] && wanted+=("$extra")
    extra=$(pkg_for "qml$QT_MAJOR"); [ -n "$extra" ] && wanted+=("$extra")
    local filtered=(); for p in "${wanted[@]}"; do [ -n "$p" ] && filtered+=("$p"); done
    printf '    installing: %s\n' "${filtered[*]}"
    pkg_install "${filtered[@]}" || warn "some packages failed; continuing"

    step "Python environment"
    make_venv
    "$PREFIX/bin/pip" install --quiet qat || fail "could not install qat"
    "$PREFIX/bin/pip" install --quiet --force-reinstall --no-deps "$WHEEL" \
        || fail "could not install $WHEEL"
    ok "qat + qat_recorder installed"

    step "Event filter"
    if "$PREFIX/bin/python" -m qat_recorder build-filter --out "$FILTER_DIR" \
            > /tmp/qatrec-build.log 2>&1; then
        LIB=$(ls "$FILTER_DIR"/libqatrec*.so 2>/dev/null | head -1)
        ok "built $LIB"
    else
        warn "the filter did not build — see /tmp/qatrec-build.log"
        warn "audit and replay still work; recording needs the filter"
        LIB=""
    fi

    [ "$WITH_AGENT" -eq 1 ] && setup_agent

    step "Done"
    cat <<EOF

  Audit an application (no filter needed):
    $PREFIX/bin/python -m qat_recorder audit --launch /path/to/app --pause

EOF
    if [ -n "${LIB:-}" ]; then
        cat <<EOF
  Record 30 seconds:
    $PREFIX/bin/python -m qat_recorder record \\
        --lib $LIB \\
        --app /path/to/app --seconds 30 --out ./recorded

EOF
    fi
}

setup_agent() {
    step "Agent (remote recording)"
    local conf="$HOME/.qatrec"
    mkdir -p "$conf"; chmod 700 "$conf"

    if [ ! -s "$conf/token" ]; then
        "$PREFIX/bin/python" -m qat_recorder.agent.cli --generate-token > "$conf/token"
        chmod 600 "$conf/token"
    fi
    if [ ! -s "$conf/agent.crt" ]; then
        "$PREFIX/bin/python" -m qat_recorder.agent.cli --generate-cert \
            --cert "$conf/agent.crt" --key "$conf/agent.key" \
            --common-name "$(hostname -f 2>/dev/null || hostname)" > /dev/null
    fi
    local fingerprint
    fingerprint=$("$PREFIX/bin/python" -c \
        "from qat_recorder.agent.security import fingerprint; print(fingerprint('$conf/agent.crt'))")
    local address
    address=$(hostname -I 2>/dev/null | awk '{print $1}')

    ok "token and certificate in $conf"
    cat <<EOF

  Start the agent (foreground, to try it):
    $PREFIX/bin/python -m qat_recorder.agent.cli \\
        --token-file $conf/token \\
        --cert $conf/agent.crt --key $conf/agent.key \\
        --bind 0.0.0.0 --port 8765

  Then on the tester's machine:
    qat-recorder hosts add ${HOSTNAME:-vm} ${address:-<this-ip>}:8765 \\
        --fingerprint $fingerprint \\
        --token-file <copy of $conf/token>

  Remember: recording is interactive, so the tester must be able to SEE this
  machine's screen (VNC or X forwarding).
EOF
}

cmd_local() {
    step "Machine"
    detect_os
    ok "$OS_NAME (tester's machine — no Qat, no filter needed)"
    find_wheel
    ok "wheel: $WHEEL"

    step "Python environment"
    make_venv
    "$PREFIX/bin/pip" install --quiet "$WHEEL" || fail "could not install $WHEEL"
    "$PREFIX/bin/pip" install --quiet PySide6-Essentials \
        || warn "PySide6 did not install; the panel needs it, the CLI does not"
    ok "qat_recorder installed"

    step "Done"
    cat <<EOF

  Register a VM you want to record on:
    $PREFIX/bin/qat-recorder hosts add vm-01 <ip>:8765 \\
        --fingerprint <printed by the agent> --token-file ~/.qatrec/token

  Then:
    $PREFIX/bin/qat-recorder hosts check vm-01
    $PREFIX/bin/qat-recorder panel --agent vm-01

EOF
}

# ---------------------------------------------------------------------------
# Uninstall
#
# This command destroyed a user's repository once. The bug: it asked Qat where
# its configuration lived, and Qat answered with a path in the CURRENT WORKING
# DIRECTORY -- because that is where it writes applications.json, not a config
# directory. The script took that path's PARENT and removed it.
#
# The rules that follow exist because of that:
#   * directories to remove are a fixed list, never derived from anything;
#   * every one is checked to be under $HOME before it is touched;
#   * config files found by asking Qat are removed as FILES, never as parents.
# ---------------------------------------------------------------------------

cmd_uninstall() {
    step "Removing"

    local qat_config_file=""
    if [ -x "$PREFIX/bin/python" ]; then
        qat_config_file=$("$PREFIX/bin/python" -c \
            "import qat; print(qat.get_config_file())" 2>/dev/null)
    fi

    local xdg="${XDG_CONFIG_HOME:-$HOME/.config}"
    local candidates=(
        "$PREFIX" "$FILTER_DIR"
        "$xdg/qat" "$HOME/.local/share/qat" "$HOME/.cache/qat"
        "$xdg/qat-recorder" "$HOME/.qatrec"
    )

    # Refuse anything outside $HOME, and refuse $HOME itself.
    local targets=()
    for candidate in "${candidates[@]}"; do
        case "$candidate" in
            "$HOME"|"$HOME/"|"/"|"") continue ;;
            "$HOME"/*) targets+=("$candidate") ;;
            *) warn "skipping $candidate (outside your home directory)" ;;
        esac
    done

    # Individual files only. Never a directory derived from a path Qat reported.
    local files=()
    case "$qat_config_file" in
        *applications.json) [ -f "$qat_config_file" ] && files+=("$qat_config_file") ;;
    esac
    [ -f ./applications.json ] && files+=("./applications.json")
    [ -f ./testSettings.json ] && files+=("./testSettings.json")

    if [ "$ASSUME_YES" -ne 1 ]; then
        printf '  Directories:\n'
        for t in "${targets[@]}"; do [ -e "$t" ] && printf '    %s\n' "$t"; done
        if [ ${#files[@]} -gt 0 ]; then
            printf '  Files:\n'
            for f in "${files[@]}"; do printf '    %s\n' "$f"; done
        fi
        printf '  Continue? [y/N] '
        read -r answer
        case "$answer" in y|Y|yes) ;; *) echo "  cancelled"; exit 0 ;; esac
    fi

    pkill -f qat-recorder-agent 2>/dev/null
    for t in "${targets[@]}"; do
        [ -e "$t" ] && { rm -rf "$t" && ok "removed $t"; }
    done
    for f in "${files[@]:-}"; do
        [ -n "$f" ] && [ -f "$f" ] && { rm -f "$f" && ok "removed $f"; }
    done
    rm -f /tmp/qat_* /tmp/qatrec-* 2>/dev/null

    if [ -f /etc/systemd/system/qat-recorder-agent.service ]; then
        need_sudo
        $SUDO systemctl disable --now qat-recorder-agent 2>/dev/null
        $SUDO rm -f /etc/systemd/system/qat-recorder-agent.service
        $SUDO systemctl daemon-reload
        ok "removed the systemd service"
    fi

    step "Done"
    echo "  System packages (cmake, Qt dev) were left alone — remove them yourself if unwanted."
}

cmd_doctor() {
    step "Machine"
    detect_os; echo "    $OS_NAME (package manager: $PKG)"
    detect_qt

    step "Installed"
    if [ -x "$PREFIX/bin/python" ]; then
        ok "environment  $PREFIX"
        "$PREFIX/bin/python" -c "import qat, os; print('    qat          ' + os.path.dirname(qat.__file__))" 2>/dev/null \
            || warn "qat is NOT installed"
        "$PREFIX/bin/python" -c "import qat_recorder; print('    qat_recorder ' + qat_recorder.__version__)" 2>/dev/null \
            || warn "qat_recorder is NOT installed"
    else
        warn "no environment at $PREFIX — run: bash install.sh vm"
    fi

    local lib; lib=$(ls "$FILTER_DIR"/libqatrec*.so 2>/dev/null | head -1)
    [ -n "$lib" ] && ok "filter       $lib" || warn "no event filter built (recording will not work)"

    step "Tools"
    for tool in cmake c++ python3; do
        command -v "$tool" >/dev/null && ok "$tool" || warn "$tool is missing"
    done

    step "Display"
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        ok "DISPLAY=${DISPLAY:-} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
        echo "    (recording needs a display you can see; replay does not)"
    else
        warn "no display — audit and replay work with QT_QPA_PLATFORM=offscreen,"
        warn "but recording needs a visible screen"
    fi
}

# ---------------------------------------------------------------------------

COMMAND="${1:-}"; shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --wheel)  WHEEL="$2"; shift 2 ;;
        --qt)     QT_MAJOR="$2"; shift 2 ;;
        --app)    APP="$2"; shift 2 ;;
        --prefix) PREFIX="$2"; FILTER_DIR="$PREFIX-filter"; shift 2 ;;
        --agent)  WITH_AGENT=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        *) fail "unknown option: $1" ;;
    esac
done

case "$COMMAND" in
    vm)         cmd_vm ;;
    local)      cmd_local ;;
    uninstall)  cmd_uninstall ;;
    doctor)     cmd_doctor ;;
    *)
        sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 1 ;;
esac
