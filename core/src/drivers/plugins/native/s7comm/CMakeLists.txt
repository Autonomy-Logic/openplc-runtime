# CMakeLists.txt for S7Comm Plugin
# Builds a self-contained S7Comm plugin with Snap7 library included

cmake_minimum_required(VERSION 3.10)
project(s7comm_plugin CXX C)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Determine OpenPLC root directory for finding common headers
# When building standalone: calculate from plugin location
# When building from main project: pass -DOPENPLC_ROOT=<path>
if(NOT DEFINED OPENPLC_ROOT)
    get_filename_component(OPENPLC_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../../../../" ABSOLUTE)
endif()

message(STATUS "S7Comm Plugin - OpenPLC root: ${OPENPLC_ROOT}")

# =============================================================================
# Snap7 Source Files
# =============================================================================

set(SNAP7_CORE_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/core/s7_server.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/core/s7_isotcp.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/core/s7_peer.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/core/s7_text.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/core/s7_client.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/core/s7_micro_client.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/core/s7_partner.cpp
)

set(SNAP7_SYS_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/sys/snap_msgsock.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/sys/snap_tcpsrvr.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/sys/snap_threads.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/sys/snap_sysutils.cpp
)

set(SNAP7_LIB_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/lib/snap7_libmain.cpp
)

# =============================================================================
# cJSON Library (shared utility for all native plugins)
# =============================================================================

set(CJSON_SOURCES
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native/cjson/cJSON.c
)

# =============================================================================
# Plugin Source Files
# =============================================================================

set(PLUGIN_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/s7comm_plugin.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/s7comm_config.c
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native/plugin_logger.c
    # Exports set_image_sizes, which is how this plugin declares it
    # understands per-table image sizes (RTOP-284). Without it the
    # runtime keeps the image square for every run this plugin is in.
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native/plugin_image_sizes.c
)

# =============================================================================
# Include Directories
# =============================================================================

include_directories(
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/core
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/sys
    ${CMAKE_CURRENT_SOURCE_DIR}/snap7/lib
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native/cjson
    ${OPENPLC_ROOT}/core/src/drivers
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native
    ${OPENPLC_ROOT}/core/src/lib
)

# =============================================================================
# Create Shared Library
# =============================================================================

add_library(s7comm_plugin SHARED
    ${SNAP7_CORE_SOURCES}
    ${SNAP7_SYS_SOURCES}
    ${SNAP7_LIB_SOURCES}
    ${CJSON_SOURCES}
    ${PLUGIN_SOURCES}
)

# =============================================================================
# Compiler Definitions
# =============================================================================

target_compile_definitions(s7comm_plugin PRIVATE
    # Snap7 definitions
    $<$<NOT:$<PLATFORM_ID:Windows>>:OS_UNIX>
)

# =============================================================================
# Compiler Options
# =============================================================================

target_compile_options(s7comm_plugin PRIVATE
    -fPIC
    # Suppress Snap7 warnings (it's third-party code)
    -Wno-unused-parameter
    -Wno-sign-compare
    -Wno-deprecated-declarations
    # C++-only warning flags (only apply to C++ files)
    $<$<AND:$<COMPILE_LANGUAGE:CXX>,$<CXX_COMPILER_ID:GNU>>:-Wno-class-memaccess>
    $<$<AND:$<COMPILE_LANGUAGE:CXX>,$<CXX_COMPILER_ID:Clang>>:-Wno-macro-redefined>
)

# =============================================================================
# Link Libraries
# =============================================================================

target_link_libraries(s7comm_plugin PRIVATE
    pthread
    ${CMAKE_DL_LIBS}
)

# Platform-specific libraries
if(UNIX AND NOT APPLE)
    # Linux requires librt for clock functions
    target_link_libraries(s7comm_plugin PRIVATE rt)
endif()

# =============================================================================
# Output Settings
# =============================================================================

set_target_properties(s7comm_plugin PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/plugins
    PREFIX "lib"
    OUTPUT_NAME "s7comm_plugin"
)

# On Linux, use .so extension
if(UNIX)
    set_target_properties(s7comm_plugin PROPERTIES SUFFIX ".so")
endif()

# =============================================================================
# Install Target
# =============================================================================

install(TARGETS s7comm_plugin
    LIBRARY DESTINATION lib/openplc/plugins
    RUNTIME DESTINATION lib/openplc/plugins
)
