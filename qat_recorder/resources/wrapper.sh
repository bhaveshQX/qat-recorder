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

if [ -n "${QATREC_LIB:-}" ]; then
    if [ -n "${LD_PRELOAD:-}" ]; then
        export LD_PRELOAD="${LD_PRELOAD}:${QATREC_LIB}"
    else
        export LD_PRELOAD="${QATREC_LIB}"
    fi
fi

exec "${QATREC_APP}" "$@"
