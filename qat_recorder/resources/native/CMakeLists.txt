cmake_minimum_required(VERSION 3.16)
project(qatrec LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Qt 6 preferred, Qt 5 supported. Only public API is used, so a library built
# against the OLDEST Qt in a major series stays binary compatible with every
# later minor in that series -- which is what keeps the build matrix to two
# artifacts instead of eleven.
#
# QATREC_QT_MAJOR overrides the choice. The newest Qt installed is nearly always
# the one the application uses -- but an application that ships its own Qt has
# nothing to do with what this machine has installed, and a filter built against
# the wrong major version loads without complaining and then sees none of the
# application's widgets. That one has to be said out loud rather than guessed.
set(QATREC_QT_MAJOR "" CACHE STRING
    "Qt major version to build against: 5, 6, or empty for the newest installed")

if(QATREC_QT_MAJOR)
    find_package(Qt${QATREC_QT_MAJOR} REQUIRED COMPONENTS Core Gui)
    set(QT_VERSION_MAJOR ${QATREC_QT_MAJOR})
else()
    find_package(QT NAMES Qt6 Qt5 REQUIRED COMPONENTS Core Gui)
    find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS Core Gui)
endif()
message(STATUS "qatrec: building against Qt ${QT_VERSION_MAJOR}")

add_library(qatrec SHARED qatrec.cpp)

target_link_libraries(qatrec PRIVATE
    Qt${QT_VERSION_MAJOR}::Core
    Qt${QT_VERSION_MAJOR}::Gui
    ${CMAKE_DL_LIBS}     # dlsym, for menu items -- see appendMenuItem()
)

# RTLD_DEFAULT is a GNU extension. g++ defines _GNU_SOURCE for C++ already, but
# saying so here means the build does not depend on that staying true.
target_compile_definitions(qatrec PRIVATE _GNU_SOURCE)

target_compile_options(qatrec PRIVATE -Wall -Wextra -fvisibility=hidden)

# Keep the C++ runtime inside the artifact so it does not require a newer
# libstdc++ than the target distribution ships. This is the mistake upstream
# made: their Qt 6.8+ builds need GLIBCXX_3.4.32 and therefore refuse to load on
# RHEL 9 or Ubuntu 22.04.
option(QATREC_STATIC_CXX "Statically link libstdc++ where available" ON)
if(QATREC_STATIC_CXX)
    # libstdc++ only. NOT -static-libgcc: a static libgcc pulls in its unwinder,
    # which on gcc 11+ references _dl_find_object -- a glibc 2.35 symbol. That
    # single reference raised the artifact's floor from 2.34 to 2.35 and cost
    # more portability than statically linking libgcc ever bought, since
    # libgcc_s.so.1 is present on every Linux system anyway. Measured, not
    # assumed: see native/tests/check_symbol_floor.sh.
    #
    # libstdc++.a is not installed by default on RHEL-family distributions, and
    # it only matters when the artifact has to run on OTHER machines. Building
    # on the same host you will record on does not need it, so a missing static
    # library is a note, not a failure.
    include(CheckCXXSourceCompiles)
    set(CMAKE_REQUIRED_LINK_OPTIONS -static-libstdc++)
    check_cxx_source_compiles("int main() { return 0; }" QATREC_STATIC_CXX_WORKS)
    unset(CMAKE_REQUIRED_LINK_OPTIONS)

    if(QATREC_STATIC_CXX_WORKS)
        target_link_options(qatrec PRIVATE -static-libstdc++)
    else()
        message(STATUS
            "libstdc++.a not found: linking libstdc++ dynamically. Fine for a "
            "filter used on this machine. To build one that runs on other "
            "distributions, install libstdc++-static (RHEL) or libstdc++-dev "
            "(Debian) and rebuild.")
    endif()
endif()

set_target_properties(qatrec PROPERTIES
    OUTPUT_NAME "qatrec.${QT_VERSION_MAJOR}.${Qt${QT_VERSION_MAJOR}_VERSION_MINOR}"
)

# ---------------------------------------------------------------------------
# The gate: which processes get instrumented at all.
#
# Preloaded in place of Qat's injector, it loads the injector and the filter in
# the application and nowhere else -- see qatgate.c for what goes wrong when
# every shell in a launch script gets them too. Deliberately no Qt and no C++:
# it is preloaded into /bin/sh, dirname and sed as well, where it must cost
# nothing and drag in nothing.
# ---------------------------------------------------------------------------
add_library(qatgate SHARED qatgate.c)
target_link_libraries(qatgate PRIVATE ${CMAKE_DL_LIBS})
target_compile_definitions(qatgate PRIVATE _GNU_SOURCE)
target_compile_options(qatgate PRIVATE -Wall -Wextra -fvisibility=hidden)

# ---------------------------------------------------------------------------
# A minimal native Qt application to test the filter against. Native rather than
# PySide so the test exercises the real target case: a compiled Qt binary.
# ---------------------------------------------------------------------------
option(QATREC_BUILD_TEST_APP "Build the test application" ON)
if(QATREC_BUILD_TEST_APP)
    find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS Widgets)
    add_executable(qatrec_test_app test_app.cpp)
    target_link_libraries(qatrec_test_app PRIVATE
        Qt${QT_VERSION_MAJOR}::Core
        Qt${QT_VERSION_MAJOR}::Gui
        Qt${QT_VERSION_MAJOR}::Widgets
    )
endif()
