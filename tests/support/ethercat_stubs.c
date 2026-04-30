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
