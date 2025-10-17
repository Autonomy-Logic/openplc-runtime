cmake_minimum_required(VERSION 3.10)
project(plc_application C)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Find Python development libraries
find_package(PkgConfig REQUIRED)
pkg_check_modules(PYTHON REQUIRED python3-embed)

# Include directories
include_directories(${CMAKE_SOURCE_DIR}/core/src/plc_app
                    ${CMAKE_SOURCE_DIR}/core/src/plc_app/utils
                    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib
                    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib/lib
                    ${PYTHON_INCLUDE_DIRS})

# Compiler options
add_compile_options(-Wall -Werror  -Wextra -fstack-protector-strong
    -D_FORTIFY_SOURCE=2 -O2 -Wformat
    -Werror=format-security -fPIC -fPIE)


# Step 3: Build the executable and link against the shared library
add_executable(plc_main
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/plc_main.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/utils/log.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/utils/utils.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/utils/watchdog.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/image_tables.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/plc_state_manager.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/plcapp_manager.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/scan_cycle_manager.c
    ${CMAKE_SOURCE_DIR}/core/src/drivers/plugin_driver.c
    ${CMAKE_SOURCE_DIR}/core/src/drivers/plugin_config.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/unix_socket.c
    ${CMAKE_SOURCE_DIR}/core/src/plc_app/debug_handler.c
)

# Link against shared library
target_link_libraries(plc_main 
    dl 
    pthread 
    ${PYTHON_LIBRARIES}
)

# Ensure executable can find shared library at runtime
set_target_properties(plc_main PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
)
