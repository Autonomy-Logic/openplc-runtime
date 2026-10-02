/**
 * The fourteen image table lengths, for a native plugin (RTOP-284).
 *
 * Linking this file into a plugin gives it two things at once: the exported
 * `set_image_sizes` symbol the runtime looks for -- whose PRESENCE is how a
 * plugin declares it understands per-table sizes -- and the accessor to read
 * back what it was told.
 *
 * One implementation rather than one per plugin, for the same reason the
 * logger is shared: three copies of a fourteen-element cache is three places
 * for the indexing to drift, and the whole point of this work is that the
 * tables no longer share a length.
 *
 * A plugin that does NOT link this is not broken. The runtime keeps the image
 * square for that run and says which plugin forced it.
 */

#ifndef PLUGIN_IMAGE_SIZES_H
#define PLUGIN_IMAGE_SIZES_H

#include "../../../plc_app/image_table_id.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * How long one table is, in its own elements.
 *
 * Returns 0 before the runtime has delivered the sizes and for an id this
 * build does not know, which are the same answer for a caller: an area it
 * cannot index into. Bounding against 0 refuses every access, which is the
 * safe direction for a plugin asked to act before it has been told anything.
 */
uint32_t plugin_image_table_capacity(image_table_id_t id);

/** Whether the runtime has delivered the sizes for this load yet. */
int plugin_image_sizes_known(void);

#ifdef __cplusplus
}
#endif

#endif /* PLUGIN_IMAGE_SIZES_H */
