/*
 * qatgate.c -- which processes get instrumented, and which are left alone.
 *
 * Qat injects by setting LD_PRELOAD to its injector before launching the
 * application. LD_PRELOAD is inherited by every descendant, so when the thing
 * being launched is a *launch script*, the injector is loaded into every
 * process that script runs -- /bin/sh, dirname, sed, the helper daemons -- and
 * it announces itself on stdout in each one:
 *
 *     Loading injector
 *     Detected Qt version 5.15.3
 *     Waiting for Qt libraries to be loaded before injecting server
 *
 * A shell does not care what a program prints. `cd $(dirname "$0")/..` does:
 * the substitution captures that chatter, and the script then tries to change
 * into a directory called "Loading injector\nDetected Qt version 5.15.3...".
 * Every relative path after it is wrong and the application never starts. Not
 * a hypothetical -- observed on two lines of one customer's launch script and
 * on the first line of each of the four helper scripts it starts.
 *
 * So this library is preloaded *instead of* the injector, and it decides. It
 * loads the injector (and the recorder's event filter) in the process Qat
 * launched, and in any descendant that has Qt loaded -- the application itself,
 * however many shells deep it is. Everywhere else it returns having loaded
 * nothing, allocated nothing, and above all written nothing to stdout.
 *
 * It links libc and libdl only. It must be loadable into /bin/sh a hundred
 * times a second without being noticed, which a library that drags in Qt is
 * not.
 *
 * Set by wrapper.sh:
 *   QATREC_PRELOAD      colon-separated libraries to load, injector first
 *   QATREC_PID          the process Qat launched; always instrumented
 *   QATREC_GATE_DEBUG   1 to narrate the decision on stderr
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

/* Longer than any plausible pair of library paths, and small enough to sit on
 * the stack of a process that is about to exec something else. */
#define QATREC_PRELOAD_MAX 4096

static int debugging(void)
{
    const char *value = getenv("QATREC_GATE_DEBUG");
    return value != NULL && value[0] == '1';
}

/* Always fd 2, never fd 1. stdout is what a command substitution captures, and
 * writing to it is the entire bug this file exists to avoid. */
static void say(const char *what, const char *detail)
{
    ssize_t ignored;

    ignored = write(2, "qatrec-gate: ", 13);
    ignored = write(2, what, strlen(what));
    if (detail != NULL) {
        ignored = write(2, ": ", 2);
        ignored = write(2, detail, strlen(detail));
    }
    ignored = write(2, "\n", 1);
    (void) ignored;
}

/* The process Qat started. `exec` keeps the pid, so this is still true after
 * wrapper.sh has handed over to the application -- and it is what makes an
 * interpreter (python running a PySide application) instrumented even before
 * it has loaded any Qt. */
static int is_the_launched_process(void)
{
    const char *wanted = getenv("QATREC_PID");

    if (wanted == NULL || wanted[0] == '\0') {
        return 0;
    }
    return (long) getpid() == strtol(wanted, NULL, 10);
}

/* A shell is never the application, whatever it was asked to run.
 *
 * The pid rule below is for an application that IS the launched process, or an
 * interpreter about to load Qt. When the launched process is a launch script,
 * the launched process is a shell -- and injecting there does active harm,
 * because the injector prints OnUnload from a destructor and a shell forks for
 * every command substitution. A substitution made of builtins exits its fork
 * without exec'ing, the destructor runs, and the script's own idea of where it
 * lives comes back with OnUnload on the end of it.
 *
 * The recorder says so through QATREC_APP_IS_SCRIPT as well. This is here
 * because an invariant worth holding is worth holding where it cannot be
 * forgotten: a shell has no Qt, so it has nothing to offer a recording. */
static int is_a_shell(void)
{
    static const char *const shells[] = {
        "sh", "bash", "dash", "ksh", "mksh", "zsh", "ash", "busybox", NULL,
    };
    char path[512];
    const char *name;
    ssize_t length;
    int index;

    length = readlink("/proc/self/exe", path, sizeof path - 1);
    if (length <= 0) {
        return 0;
    }
    path[length] = '\0';

    name = strrchr(path, '/');
    name = (name != NULL) ? name + 1 : path;
    for (index = 0; shells[index] != NULL; index++) {
        if (strcmp(name, shells[index]) == 0) {
            return 1;
        }
    }
    return 0;
}

/* RTLD_NOLOAD answers "is this already mapped?" without loading anything and
 * without touching the filesystem. By the time a preloaded constructor runs,
 * every library the executable links is mapped, so an application that links Qt
 * answers yes here however deep in a tree of shells it was started. */
static int qt_is_loaded(void)
{
    static const char *const sonames[] = {
        "libQt6Core.so.6",
        "libQt5Core.so.5",
        "libQtCore.so.4",
        NULL,
    };
    int index;

    for (index = 0; sonames[index] != NULL; index++) {
        void *handle = dlopen(sonames[index], RTLD_LAZY | RTLD_NOLOAD);
        if (handle != NULL) {
            dlclose(handle);
            return 1;
        }
    }

    /* Whatever the file is called, QtCore exports qVersion(). */
    return dlsym(RTLD_DEFAULT, "_Z8qVersionv") != NULL;
}

__attribute__((constructor))
static void qatrec_gate(void)
{
    char libraries[QATREC_PRELOAD_MAX];
    char *remaining = NULL;
    char *each;
    const char *list = getenv("QATREC_PRELOAD");

    if (list == NULL || list[0] == '\0') {
        return;
    }

    if (!qt_is_loaded() && !(is_the_launched_process() && !is_a_shell())) {
        if (debugging()) {
            say("no Qt here, loading nothing", NULL);
        }
        return;
    }

    if (strlen(list) >= sizeof libraries) {
        say("QATREC_PRELOAD is too long to use", NULL);
        return;
    }
    strcpy(libraries, list);

    /* Lazy binding into the global scope: the terms ld.so would have loaded
     * these on as LD_PRELOAD entries, which is what they were written for. */
    for (each = strtok_r(libraries, ":", &remaining); each != NULL;
         each = strtok_r(NULL, ":", &remaining)) {
        if (each[0] == '\0') {
            continue;
        }
        if (dlopen(each, RTLD_LAZY | RTLD_GLOBAL) == NULL) {
            const char *why = dlerror();
            say("could not load", each);
            if (why != NULL) {
                say("  reason", why);
            }
        } else if (debugging()) {
            say("loaded", each);
        }
    }
}
