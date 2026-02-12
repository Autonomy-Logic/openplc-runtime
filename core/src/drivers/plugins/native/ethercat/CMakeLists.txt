# CMakeLists.txt for EtherCAT Plugin
# Builds a self-contained EtherCAT plugin with SOEM library included

cmake_minimum_required(VERSION 3.10)
project(ethercat_plugin C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Determine OpenPLC root directory for finding common headers
# When building standalone: calculate from plugin location
# When building from main project: pass -DOPENPLC_ROOT=<path>
if(NOT DEFINED OPENPLC_ROOT)
    get_filename_component(OPENPLC_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../../../../" ABSOLUTE)
endif()

message(STATUS "EtherCAT Plugin - OpenPLC root: ${OPENPLC_ROOT}")

# =============================================================================
# SOEM Library (built via add_subdirectory for proper ec_options.h generation)
# =============================================================================

set(SOEM_DIR ${CMAKE_CURRENT_SOURCE_DIR}/libs/soem)
set(SOEM_BUILD_SAMPLES OFF CACHE BOOL "Disable SOEM samples" FORCE)

# On MSYS2/Cygwin, SOEM ships no platform cmake file (only Linux.cmake,
# Windows.cmake, rt-kernel.cmake exist).  Generate one from the Win32
# variant because MSYS2 can use Win32 OSAL/OSHW through w32api headers
# while the Linux OSHW needs AF_PACKET raw sockets which are unavailable.
if(CMAKE_SYSTEM_NAME STREQUAL "MSYS" OR CMAKE_SYSTEM_NAME STREQUAL "CYGWIN")
    set(_SOEM_PLATFORM_CMAKE "${SOEM_DIR}/cmake/${CMAKE_SYSTEM_NAME}.cmake")
    message(STATUS "EtherCAT Plugin - Generating ${CMAKE_SYSTEM_NAME}.cmake for SOEM (Win32 OSAL/OSHW via w32api)")
    file(WRITE "${_SOEM_PLATFORM_CMAKE}" "\
# Auto-generated for ${CMAKE_SYSTEM_NAME} -- based on Windows.cmake\n\
# Uses Win32 OSAL/OSHW accessible through w32api on MSYS2/Cygwin.\n\
\n\
target_sources(soem PRIVATE\n\
  osal/win32/osal.c\n\
  osal/win32/osal_defs.h\n\
  oshw/win32/oshw.c\n\
  oshw/win32/oshw.h\n\
  oshw/win32/nicdrv.c\n\
  oshw/win32/nicdrv.h\n\
)\n\
\n\
target_include_directories(soem PUBLIC\n\
  $<BUILD_INTERFACE:\${SOEM_SOURCE_DIR}/osal/win32>\n\
  $<BUILD_INTERFACE:\${SOEM_SOURCE_DIR}/oshw/win32>\n\
  $<BUILD_INTERFACE:\${SOEM_SOURCE_DIR}/oshw/win32/wpcap/Include>\n\
  $<INSTALL_INTERFACE:include/soem>\n\
)\n\
\n\
target_compile_options(soem PUBLIC\n\
  $<$<C_COMPILER_ID:GNU>:-std=c11>\n\
)\n\
\n\
# WIN32 is NOT predefined by MSYS2/Cygwin GCC, but the bundled\n\
# wpcap headers (pcap/pcap.h:40) need it to route to pcap-stdinc.h.\n\
# HAVE_U_INT*_T tells bundled bittypes.h to skip its typedefs --\n\
# MSYS2 POSIX headers (sys/types.h) already provide these types and\n\
# use different underlying types on LP64 (long vs long long for 64-bit).\n\
target_compile_definitions(soem PUBLIC\n\
  WIN32\n\
  HAVE_U_INT8_T\n\
  HAVE_U_INT16_T\n\
  HAVE_U_INT32_T\n\
  HAVE_U_INT64_T\n\
)\n\
\n\
foreach(target IN ITEMS\n\
    soem\n\
    ec_sample\n\
    eepromtool\n\
    eni_test\n\
    eoe_test\n\
    firm_update\n\
    simple_ng\n\
    slaveinfo)\n\
  if (TARGET \${target})\n\
    target_compile_options(\${target} PRIVATE\n\
      $<$<C_COMPILER_ID:GNU>:\n\
      -Wall\n\
      -Wextra\n\
      -Wno-unused-parameter\n\
      >\n\
    )\n\
  endif()\n\
endforeach()\n\
\n\
if(CMAKE_SIZEOF_VOID_P EQUAL 8)\n\
  set(WPCAP_LIB_PATH \${SOEM_SOURCE_DIR}/oshw/win32/wpcap/Lib/x64)\n\
  target_link_libraries(soem PUBLIC\n\
    \${WPCAP_LIB_PATH}/wpcap.lib\n\
    \${WPCAP_LIB_PATH}/Packet.lib\n\
  )\n\
elseif(CMAKE_SIZEOF_VOID_P EQUAL 4)\n\
  set(WPCAP_LIB_PATH \${SOEM_SOURCE_DIR}/oshw/win32/wpcap/Lib)\n\
  target_link_libraries(soem PUBLIC\n\
    \${WPCAP_LIB_PATH}/libwpcap.a\n\
    \${WPCAP_LIB_PATH}/libpacket.a\n\
  )\n\
endif()\n\
\n\
target_link_libraries(soem PUBLIC ws2_32 winmm)\n\
\n\
install(FILES\n\
  osal/win32/osal_defs.h\n\
  oshw/win32/nicdrv.h\n\
  DESTINATION include/soem\n\
)\n\
")
    unset(_SOEM_PLATFORM_CMAKE)
endif()

add_subdirectory(${SOEM_DIR} ${CMAKE_CURRENT_BINARY_DIR}/soem)

# =============================================================================
# cJSON Library (embedded for JSON configuration parsing)
# =============================================================================

set(CJSON_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/cjson/cJSON.c
)

# =============================================================================
# Plugin Source Files
# =============================================================================

set(PLUGIN_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/ethercat_plugin.c
    ${CMAKE_CURRENT_SOURCE_DIR}/ethercat_config.c
    ${CMAKE_CURRENT_SOURCE_DIR}/ethercat_master.c
    ${CMAKE_CURRENT_SOURCE_DIR}/ethercat_io.c
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native/plugin_logger.c
)

# =============================================================================
# Create Shared Library
# =============================================================================

add_library(ethercat_plugin SHARED
    ${CJSON_SOURCES}
    ${PLUGIN_SOURCES}
)

# =============================================================================
# Include Directories
# =============================================================================

target_include_directories(ethercat_plugin PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/cjson
    ${OPENPLC_ROOT}/core/src/drivers
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native
    ${OPENPLC_ROOT}/core/src/lib
)

# =============================================================================
# Compiler Options
# =============================================================================

target_compile_options(ethercat_plugin PRIVATE
    -fPIC
    -Wno-unused-parameter
    -Wno-sign-compare
)

# =============================================================================
# Link Libraries
# =============================================================================

target_link_libraries(ethercat_plugin PRIVATE
    soem
    ${CMAKE_DL_LIBS}
)

# =============================================================================
# Output Settings
# =============================================================================

set_target_properties(ethercat_plugin PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/plugins
    PREFIX "lib"
    OUTPUT_NAME "ethercat_plugin"
)

# On Linux, use .so extension
if(UNIX)
    set_target_properties(ethercat_plugin PROPERTIES SUFFIX ".so")
endif()

# =============================================================================
# Install Target
# =============================================================================

install(TARGETS ethercat_plugin
    LIBRARY DESTINATION lib/openplc/plugins
    RUNTIME DESTINATION lib/openplc/plugins
)
