#include "image_tables.h"
#include "journal_buffer.h"
#include "plugin_config.h"
#include "plugin_driver.h"

#include <pthread.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Stub: base_tick_ns (utils.c) -- the runtime stores the PLC scan tick
// interval here (GCD of declared task intervals). Plugin drivers
// (plugin_driver.c) read it during arg construction, so a default
// stub value is enough for the unit tests.
uint64_t base_tick_ns = 0;

// Stub storage for the image tables (defined in image_tables.cpp in the real
// build). One symbol rather than fourteen, and it takes its shape from
// image_tables.h -- which is the point: the stub used to spell the fourteen
// arrays out by hand at BUFFER_SIZE=128 (project.yml) while plugin_driver.c
// saw them at 1024, a disagreement the linker was happy to accept.
//
// The tables are heap pointers in the real build, allocated per program load.
// Here they point at fixed arrays of STUB_IMAGE_ELEMENTS, which is all the
// plugin_driver tests need: they check that the runtime args are populated,
// not that the image is the right size.
image_tables_t g_image;

#define STUB_IMAGE_ELEMENTS 128

static IEC_BOOL *stub_bool_input[STUB_IMAGE_ELEMENTS][8];
static IEC_BOOL *stub_bool_output[STUB_IMAGE_ELEMENTS][8];
static IEC_BOOL *stub_bool_memory[STUB_IMAGE_ELEMENTS][8];
static IEC_BYTE *stub_byte_input[STUB_IMAGE_ELEMENTS];
static IEC_BYTE *stub_byte_output[STUB_IMAGE_ELEMENTS];
static IEC_UINT *stub_int_input[STUB_IMAGE_ELEMENTS];
static IEC_UINT *stub_int_output[STUB_IMAGE_ELEMENTS];
static IEC_UDINT *stub_dint_input[STUB_IMAGE_ELEMENTS];
static IEC_UDINT *stub_dint_output[STUB_IMAGE_ELEMENTS];
static IEC_ULINT *stub_lint_input[STUB_IMAGE_ELEMENTS];
static IEC_ULINT *stub_lint_output[STUB_IMAGE_ELEMENTS];
static IEC_UINT *stub_int_memory[STUB_IMAGE_ELEMENTS];
static IEC_UDINT *stub_dint_memory[STUB_IMAGE_ELEMENTS];
static IEC_ULINT *stub_lint_memory[STUB_IMAGE_ELEMENTS];

// Stub: image_tables_alloc (image_tables.cpp). Points the tables at the fixed
// storage above and ignores the requested sizes -- there is no allocator here
// to exercise. plugin_driver.c refuses to build runtime args while the
// capacity is zero, which is the ordering invariant it now enforces, so a test
// that wants args has to call this first exactly as the real load path does.
//
// TAKES THE SIZES STRUCT, not a single count (RTOP-284). The signature moved
// when the fourteen tables stopped sharing a length, and this stub kept the
// old one -- so the C unit tests did not compile at all on this branch.
// Nothing caught it because the Ceedling suite does not run in CI, and it
// would not even configure until the SOEM submodule and its generated
// ec_options.h were in place. Running it by hand is what surfaced this.
bool image_tables_alloc(const image_sizes_t *sizes)
{
    (void)sizes;
    g_image.bool_input  = stub_bool_input;
    g_image.bool_output = stub_bool_output;
    g_image.bool_memory = stub_bool_memory;
    g_image.byte_input  = stub_byte_input;
    g_image.byte_output = stub_byte_output;
    g_image.int_input   = stub_int_input;
    g_image.int_output  = stub_int_output;
    g_image.dint_input  = stub_dint_input;
    g_image.dint_output = stub_dint_output;
    g_image.lint_input  = stub_lint_input;
    g_image.lint_output = stub_lint_output;
    g_image.int_memory  = stub_int_memory;
    g_image.dint_memory = stub_dint_memory;
    g_image.lint_memory = stub_lint_memory;
    return true;
}

void image_tables_free(void)
{
    memset(&g_image, 0, sizeof(g_image));
}

uint32_t image_tables_capacity(void)
{
    return g_image.byte_input ? STUB_IMAGE_ELEMENTS : 0u;
}

// Stub: image_table_capacity (image_tables.cpp). Every stub table is the same
// fixed length, so the per-table answer is the same as the single-number one.
// That is a property of the stub and not of the runtime, where the whole point
// is that the fourteen differ -- a test about per-table lengths belongs against
// the real allocator, not here.
uint32_t image_table_capacity(image_table_id_t id)
{
    (void)id;
    return g_image.byte_input ? STUB_IMAGE_ELEMENTS : 0u;
}

// Stub: plugin_manager_destroy (plcapp_manager.c)
void plugin_manager_destroy(PluginManager *manager)
{
    (void)manager;
}

// Stub: init_rt_mutex (utils.c) - weak so tests can override with their own mock
__attribute__((weak)) int init_rt_mutex(pthread_mutex_t *mutex)
{
    (void)mutex;
    return 0;
}

// Stub: journal_write_* (journal_buffer.c)
int journal_write_bool(journal_buffer_type_t type, uint16_t index, uint8_t bit, bool value)
{
    (void)type;
    (void)index;
    (void)bit;
    (void)value;
    return 0;
}

int journal_write_byte(journal_buffer_type_t type, uint16_t index, uint8_t value)
{
    (void)type;
    (void)index;
    (void)value;
    return 0;
}

int journal_write_int(journal_buffer_type_t type, uint16_t index, uint16_t value)
{
    (void)type;
    (void)index;
    (void)value;
    return 0;
}

int journal_write_dint(journal_buffer_type_t type, uint16_t index, uint32_t value)
{
    (void)type;
    (void)index;
    (void)value;
    return 0;
}

int journal_write_lint(journal_buffer_type_t type, uint16_t index, uint64_t value)
{
    (void)type;
    (void)index;
    (void)value;
    return 0;
}

// The MatIEC-era flat-index API (get_var_list / get_var_size /
// get_var_count from plugin_utils.c) was removed alongside the rest of
// the MatIEC pipeline. Plugins now receive structured runtime args
// (plugin_runtime_args_t) constructed from the STruC++ debug map; the
// debugger ABI is exercised in test_debug_handler.c.

// Stubs: plc_tasks_reader_lock / plc_tasks_reader_unlock (plc_state_manager.cpp).
// scan_cycle_manager.c calls these around format_timing_stats_response to
// keep the reader from racing the bootstrap thread freeing plc_tasks. The
// real lock lives in plc_state_manager.cpp; tests don't pull that .cpp in,
// so we provide no-op stubs. Tests that exercise the lifecycle (rather
// than just the per-tracker math) will need to link the real symbols.
void plc_tasks_reader_lock(void) {}
void plc_tasks_reader_unlock(void) {}

// Stub: log_* (log.c)
void log_info(const char *fmt, ...)
{
    (void)fmt;
}

void log_debug(const char *fmt, ...)
{
    (void)fmt;
}

void log_warn(const char *fmt, ...)
{
    (void)fmt;
}

void log_error(const char *fmt, ...)
{
    (void)fmt;
}
