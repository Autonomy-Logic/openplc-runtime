/**
 * @file ethercat_stubs.c
 * @brief Link-time stubs for symbols referenced by ethercat_io.c /
 *        ethercat_master.c that are not exercised in unit tests.
 *
 * These let test binaries link without dragging in the entire EtherCAT
 * master (which depends on SOEM + a live network interface). Tests that
 * actually want to verify behavior of plugin_logger or ecat_master can
 * provide their own non-weak definitions to override these.
 */

#include "ethercat_config.h"
#include "ethercat_master.h"
#include "plugin_image_sizes.h"
#include "plugin_logger.h"

#include <stdarg.h>
#include <stdint.h>

/* ---- plugin_logger: variadic, no-op ---- */

__attribute__((weak)) void plugin_logger_info(plugin_logger_t *logger, const char *fmt, ...)
{
    (void)logger;
    (void)fmt;
}

__attribute__((weak)) void plugin_logger_warn(plugin_logger_t *logger, const char *fmt, ...)
{
    (void)logger;
    (void)fmt;
}

__attribute__((weak)) void plugin_logger_error(plugin_logger_t *logger, const char *fmt, ...)
{
    (void)logger;
    (void)fmt;
}

__attribute__((weak)) void plugin_logger_debug(plugin_logger_t *logger, const char *fmt, ...)
{
    (void)logger;
    (void)fmt;
}

/* ---- ecat_master accessors: return safe defaults ---- */

__attribute__((weak)) uint8_t *ecat_master_get_iomap(ecat_master_instance_t *inst)
{
    (void)inst;
    return NULL;
}

__attribute__((weak)) const ec_slavet *ecat_master_get_slave(ecat_master_instance_t *inst,
                                                             int position)
{
    (void)inst;
    (void)position;
    return NULL;
}

__attribute__((weak)) size_t ecat_master_get_iomap_size(ecat_master_instance_t *inst)
{
    (void)inst;
    return 0;
}

__attribute__((weak)) int ecat_master_get_slave_count(ecat_master_instance_t *inst)
{
    (void)inst;
    return 0;
}

/* ---- ecat_data_type_to_string: real one lives in ethercat_data_types.c ----
 *
 * ethercat_io.c logs the type name on three paths and no test target compiles
 * that file, so every link of ethercat_io.c failed on it. Pre-existing: the
 * same undefined reference happens on development. Only reached by log lines,
 * so a fixed string keeps the tests honest without pulling the table in. */
__attribute__((weak)) const char *ecat_data_type_to_string(ecat_data_type_t dt)
{
    (void)dt;
    return "<stub>";
}

/* ---- plugin_image_sizes: the shared helper the runtime hands the plugins ----
 *
 * `ecat_io_build_channel_map` asks these two how far a table reaches
 * (RTOP-284). The real definitions are in plugin_image_sizes.c, which no test
 * target compiles -- the EtherCAT tests link against stubs rather than the
 * plugin's own sources, which is what this whole file is for.
 *
 * NOT KNOWN, so the caller takes its `buffer_size` fallback: that is the shape
 * a run has when no runtime ever delivered the per-table sizes, which is the
 * conservative side and the one these tests were written against. */
__attribute__((weak)) int plugin_image_sizes_known(void)
{
    return 0;
}

__attribute__((weak)) uint32_t plugin_image_table_capacity(image_table_id_t id)
{
    (void)id;
    return 0;
}
