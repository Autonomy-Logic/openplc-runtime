#include "plugin_image_sizes.h"

#include <string.h>

/* Zeroed until the runtime calls set_image_sizes(), which it does once per
 * plugin_driver_init() and therefore once per program load. Deliberately NOT
 * remembered across loads: a cached copy from the previous program is exactly
 * the stale state the per-load delivery exists to prevent. */
static uint32_t g_sizes[IMAGE_TABLE_COUNT];
static int      g_known = 0;

/**
 * Exported for the runtime to find by dlsym. Its presence is the declaration.
 *
 * `count` is how many entries the runtime sent, which need not be
 * IMAGE_TABLE_COUNT: a plugin built against an older enum reads the prefix it
 * knows and ignores the rest, and one built against a newer enum leaves the
 * tail at zero rather than reading past the array.
 */
int set_image_sizes(const uint32_t *sizes, uint32_t count)
{
    memset(g_sizes, 0, sizeof(g_sizes));
    if (!sizes) return -1;

    const uint32_t n = count < (uint32_t)IMAGE_TABLE_COUNT ? count : (uint32_t)IMAGE_TABLE_COUNT;
    for (uint32_t i = 0; i < n; ++i) g_sizes[i] = sizes[i];
    g_known = 1;
    return 0;
}

uint32_t plugin_image_table_capacity(image_table_id_t id)
{
    if (id < 0 || id >= IMAGE_TABLE_COUNT) return 0;
    return g_sizes[id];
}

int plugin_image_sizes_known(void) { return g_known; }
