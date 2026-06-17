cmake_minimum_required(VERSION 3.10)
project(plc_application CXX C)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Find Python development libraries
find_package(PkgConfig REQUIRED)
pkg_check_modules(PYTHON REQUIRED python3-embed)

# Include directories.
#
# Runtime-side strucpp assets live in:
#   core/src/lib/strucpp_abi.hpp     — ABI mirror used to walk the .so
# We do NOT vendor strucpp's full runtime headers into the runtime —
# those ship with each user-program upload and are only consumed by
# scripts/compile.sh when building the .so.
include_directories(${CMAKE_SOURCE_DIR}/core/src/plc_app
                    ${CMAKE_SOURCE_DIR}/core/src/plc_app/utils
                    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib
                    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib/lib
                    ${PYTHON_INCLUDE_DIRS})

# Compiler options
add_compile_options(-Wall -Werror -Wextra -fstack-protector-strong
    -D_FORTIFY_SOURCE=2 -O2 -Wformat
    -Werror=format-security -fPIC -fPIE)

# On Cygwin/MSYS2, disable treating warnings as errors due to header conflicts
# The _POSIX_C_SOURCE redefinition warning is caused by Python headers vs system headers
if(CMAKE_SYSTEM_NAME MATCHES "CYGWIN|MSYS")
    add_compile_options(-Wno-error)
endif()

# Step 3: Build the executable and link against the shared library
add_executable(plc_main
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/plc_main.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/utils/log.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/utils/utils.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/utils/watchdog.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/image_tables.cpp
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/journal_buffer.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/debug_write_journal.cpp
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/plc_io_cycle.cpp
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/plc_state_manager.cpp
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/plcapp_manager.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/scan_cycle_manager.c
    ${CMAKE_SOURCE_DIR}/core/src/drivers/plugin_driver.c
    ${CMAKE_SOURCE_DIR}/core/src/drivers/plugin_config.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/unix_socket.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/debug_handler.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/client_tcp_udp.c
)

# Link against shared library
target_link_libraries(plc_main
    dl
    pthread
    ${PYTHON_LIBRARIES}
)

# Some 64-bit atomic operations (e.g. std::atomic<uint64_t> in the scan-cycle
# bookkeeping) are lowered to libatomic calls on 32-bit targets such as armv7
# (Raspberry Pi OS 32-bit / Raspbian), producing link errors like:
#   undefined reference to '__atomic_load_8' ... libatomic.so.1: DSO missing
# On 64-bit targets (arm64, x86_64) the compiler inlines them and libatomic is
# often absent, so detect-and-link only when it is actually required.
include(CheckCXXSourceCompiles)
set(_OPENPLC_ATOMIC_TEST
    "#include <atomic>\n#include <cstdint>\nint main(){ std::atomic<uint64_t> a{0}; a.fetch_add(1); return (int)a.load(); }")
check_cxx_source_compiles("${_OPENPLC_ATOMIC_TEST}" OPENPLC_ATOMICS_BUILTIN)
if(NOT OPENPLC_ATOMICS_BUILTIN)
    set(CMAKE_REQUIRED_LIBRARIES atomic)
    check_cxx_source_compiles("${_OPENPLC_ATOMIC_TEST}" OPENPLC_ATOMICS_NEED_LIBATOMIC)
    unset(CMAKE_REQUIRED_LIBRARIES)
    if(OPENPLC_ATOMICS_NEED_LIBATOMIC)
        target_link_libraries(plc_main atomic)
        message(STATUS "Linking libatomic (64-bit atomics require it on this target)")
    else()
        message(WARNING "64-bit atomics require libatomic but it was not found")
    endif()
endif()

# Export symbols from the executable so that dlopen'd PLC shared libraries
# can resolve functions provided by the runtime (e.g. TCP communication blocks)
target_link_options(plc_main PRIVATE -rdynamic)

# Ensure executable can find shared library at runtime
set_target_properties(plc_main PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
)
