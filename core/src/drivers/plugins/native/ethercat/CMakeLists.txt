# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®
#
# EtherCAT client plugin: relays process data between EtherDOG and the image tables.

cmake_minimum_required(VERSION 3.10)
project(ethercat_plugin C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT DEFINED OPENPLC_ROOT)
    get_filename_component(OPENPLC_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../../../../" ABSOLUTE)
endif()

add_library(ethercat_plugin SHARED
    ${CMAKE_CURRENT_SOURCE_DIR}/ethercat_plugin.c
    ${CMAKE_CURRENT_SOURCE_DIR}/etherdog_link.c
    ${CMAKE_CURRENT_SOURCE_DIR}/ethercat_iomap.c
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native/cjson/cJSON.c
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native/plugin_logger.c
)

target_include_directories(ethercat_plugin PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native/cjson
    ${OPENPLC_ROOT}/core/src/drivers
    ${OPENPLC_ROOT}/core/src/drivers/plugins/native
    ${OPENPLC_ROOT}/core/src/lib
)

target_compile_options(ethercat_plugin PRIVATE -Wall -Wextra -Wno-unused-parameter)
target_link_libraries(ethercat_plugin PRIVATE pthread)

# MSYS2/Cygwin: export the plugin entry points so dlsym() resolves them.
if(CMAKE_SYSTEM_NAME STREQUAL "MSYS" OR CMAKE_SYSTEM_NAME STREQUAL "CYGWIN")
    set_target_properties(ethercat_plugin PROPERTIES LINK_FLAGS "-Wl,--export-all-symbols")
endif()

set_target_properties(ethercat_plugin PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/plugins
    PREFIX "lib"
    OUTPUT_NAME "ethercat_plugin"
)
if(UNIX)
    set_target_properties(ethercat_plugin PROPERTIES SUFFIX ".so")
endif()

install(TARGETS ethercat_plugin
    LIBRARY DESTINATION lib/openplc/plugins
    RUNTIME DESTINATION lib/openplc/plugins
)
