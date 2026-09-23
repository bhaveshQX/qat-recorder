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
    # The application, not the shell that starts it.
    #
    # The gate instruments the process Qat launched as well as any descendant
    # that has Qt, which is right when that process IS the application: a
    # binary, or an interpreter that will load Qt later. A launch script is not
    # the application, and injecting into it does active harm.
    #
    # Qat's injector prints on the way out as well as on the way in: OnUnload,
    # to stdout, from a destructor. A shell forks for every `$(...)`, and a
    # substitution made of builtins -- `$(cd "$(dirname "$0")/.." && pwd)` is
    # the usual one -- exits that fork without exec'ing anything, so the
    # destructor runs and the substitution captures the word. The script's idea
    # of its own root becomes the path with OnUnload stuck on the end of it,
    # every path built from it is wrong, and the application reports that its
    # configuration files cannot be found. Nothing anywhere says injection had
    # anything to do with it.
    #
    # $$ survives the `exec` below, so where it is wanted it stays the pid Qat
    # is watching.
    if [ "${QATREC_APP_IS_SCRIPT:-0}" != "1" ]; then
        QATREC_PID=$$
        export QATREC_PID
    fi
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

# What the application is actually being started with, for comparing against a
# launch by hand. An application that starts from a terminal and not from here
# differs in exactly one of these, and reading them beats guessing.
#
# $PWD rather than $(pwd): a parameter, not a command substitution. Every line
# goes to stderr, which nothing captures.
if [ "${QATREC_GATE_DEBUG:-0}" = "1" ]; then
    {
        echo "qatrec-wrapper: cwd          ${PWD}"
        echo "qatrec-wrapper: app          ${QATREC_APP}"
        echo "qatrec-wrapper: args         $*"
        echo "qatrec-wrapper: LD_PRELOAD   ${LD_PRELOAD:-}"
        echo "qatrec-wrapper: preload      ${QATREC_PRELOAD:-}"
        echo "qatrec-wrapper: inject pid   ${QATREC_PID:-(not this process)}"
        echo "qatrec-wrapper: theme        [${QT_QPA_PLATFORMTHEME-unset}]"
    } >&2
fi

# A launch script goes through env, so that it always starts in a fresh process.
#
# This shell was started by Qat, with Qat's injector preloaded, and it is still
# loaded here: LD_PRELOAD=gate only takes effect at the next exec. When a script
# has a first line the kernel does not recognise -- a UTF-8 byte-order mark
# before the #!, from an editor on Windows -- `exec` fails with ENOEXEC and bash
# does not start a new process: it runs the script itself, in this one, with the
# injector still mapped. Every $(...) then forks a copy that prints OnUnload on
# the way out -- the gate never gets a say -- and the script's root comes back as
#
#     .../spine.Build3383 OnUnload
#
# The tell is this, on the first line of the output:
#
#     start_spine.sh: line 1: #!/bin/sh: No such file or directory
#
# env is a fresh process under the gate alone, and on ENOEXEC its execvp hands
# the script to /bin/sh as a fresh process too. A script with a good #! line
# runs exactly as before.
if [ "${QATREC_APP_IS_SCRIPT:-0}" = "1" ]; then
    exec env "${QATREC_APP}" "$@"
fi
exec "${QATREC_APP}" "$@"
