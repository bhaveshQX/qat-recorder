#!/usr/bin/env bash
# Run an application under BOTH Qat's injector and the qatrec event filter.
#
# Qat sets LD_PRELOAD to its own injector when it launches an application,
# overwriting anything already there (app_launcher.py). So the filter cannot
# simply be exported beforehand — it has to be appended from inside the launched
# process, which is what this wrapper does.
#
# `exec` keeps the same PID, so Qat's process tracking stays valid.
#
# Qat registers THIS script as the application; QATREC_LIB points at the filter
# and QATREC_APP at the real binary. Both are set by the recorder.

# Who gets the injector, and who is left completely alone.
#
# LD_PRELOAD is inherited by every process the application starts. When the
# application is a launch script that is every shell, dirname and sed in it --
# and Qat's injector announces itself on stdout in each one, so a script line
# like `cd $(dirname "$0")/..` changes into a directory called "Loading
# injector...". The gate is preloaded instead and loads the injector only where
# it belongs; qatgate.c explains it in full.
#
# Without a gate library -- an older filter build, or a machine where only the
# filter was compiled -- the original behaviour, which is correct for an
# application launched directly rather than through a script.
USE_GATE=0
if [ -n "${QATREC_GATE:-}" ] && [ -f "${QATREC_GATE}" ]; then
    USE_GATE=1
fi
if [ "${QATREC_NO_GATE:-0}" = "1" ]; then
    USE_GATE=0
fi

if [ "${USE_GATE}" = "1" ]; then
    QATREC_PRELOAD="${LD_PRELOAD:-}"
    if [ -n "${QATREC_LIB:-}" ]; then
        QATREC_PRELOAD="${QATREC_PRELOAD:+${QATREC_PRELOAD}:}${QATREC_LIB}"
    fi
    export QATREC_PRELOAD
    # $$ survives the `exec` below, so this stays the pid Qat is watching.
    QATREC_PID=$$
    export QATREC_PID
    export LD_PRELOAD="${QATREC_GATE}"
elif [ -n "${QATREC_LIB:-}" ]; then
    if [ -n "${LD_PRELOAD:-}" ]; then
        export LD_PRELOAD="${LD_PRELOAD}:${QATREC_LIB}"
    else
        export LD_PRELOAD="${QATREC_LIB}"
    fi
fi

# Ask Qt for its own file and colour dialogs rather than the desktop's.
#
# A native dialog is not a Qt widget. It belongs to GTK or to the desktop
# portal, it does not appear in the object tree, and Qat can neither see nor
# drive it. Worse, it is modal and runs its own event loop, so while it is open
# the application stops answering Qat at all -- observed as
#
#     Error sending command - trying to reconnect: timed out
#
# with the recording dead in the water from the moment the file picker opened.
# Emptying QT_QPA_PLATFORMTHEME makes Qt fall back to QFileDialog, which is a
# Qt widget tree like any other: visible, nameable, and replayable.
#
# It changes how the dialog looks, which is why it is opt-out. Recording and
# replay must agree, so the generated test sets exactly the same variable.
if [ "${QATREC_NATIVE_DIALOGS:-0}" != "1" ]; then
    export QT_QPA_PLATFORMTHEME=""
fi

# Start the application from its own directory.
#
# This is what a desktop launcher does and what a person does in a terminal,
# and some applications require it: a launch script that calls its siblings as
# ./start_storescp_pacs.sh and ./spine resolves those against the working
# directory, not against itself, so started from anywhere else every one of
# them is "No such file or directory".
#
# Parameter expansion, never a command substitution. LD_PRELOAD is already set
# when this script starts, so every subprocess it spawns loads Qat's injector
# too, and the injector writes to stdout. A substitution captures that chatter
# as part of its result, so cd was handed the whole injection log with the
# directory stuck on the end of it. No subprocess, no chatter.
case "${QATREC_APP}" in
    */*) cd "${QATREC_APP%/*}" || exit 1 ;;
esac

exec "${QATREC_APP}" "$@"
