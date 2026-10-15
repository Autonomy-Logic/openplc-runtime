/**
 * The identity of each I/O image table, and nothing else.
 *
 * Split out of image_tables.h so it can reach BOTH sides: the runtime, which
 * owns the tables, and plugin_types.h, which plugins include. A plugin that
 * exports `set_image_sizes` receives an array indexed by this enum, so it has
 * to be able to name the entries -- and the alternative, a second copy of the
 * enum in the plugin-facing header, is the drift this file exists to prevent.
 *
 * Publishing a TYPE costs no ABI: no struct gains a field and no offset moves,
 * which is what CON06 guarantees pre-compiled plugins.
 *
 * ORDER IS THE CONTRACT. It is the declaration order of `image_tables_t`, the
 * order `image.conf` is written in, and the order the sizes array arrives in.
 * A pytest checks it against the editor's list and the webserver's
 * (tests/pytest/test_image_conf_contract.py). Note that journal_buffer.h has
 * its own fourteen in a DIFFERENT order -- see kJournalToImageTable.
 */

#ifndef IMAGE_TABLE_ID_H
#define IMAGE_TABLE_ID_H

#ifdef __cplusplus
extern "C" {
#endif

/* One id per table, in the order image_tables_t declares them. Note the gap
 * the list makes visible: byte_input and byte_output exist, byte_memory does
 * not, so `%MB` has no storage on this runtime at all. */
typedef enum
{
    IMAGE_TABLE_BOOL_INPUT = 0,
    IMAGE_TABLE_BOOL_OUTPUT,
    IMAGE_TABLE_BYTE_INPUT,
    IMAGE_TABLE_BYTE_OUTPUT,
    IMAGE_TABLE_INT_INPUT,
    IMAGE_TABLE_INT_OUTPUT,
    IMAGE_TABLE_DINT_INPUT,
    IMAGE_TABLE_DINT_OUTPUT,
    IMAGE_TABLE_LINT_INPUT,
    IMAGE_TABLE_LINT_OUTPUT,
    IMAGE_TABLE_INT_MEMORY,
    IMAGE_TABLE_DINT_MEMORY,
    IMAGE_TABLE_LINT_MEMORY,
    IMAGE_TABLE_BOOL_MEMORY,
    IMAGE_TABLE_COUNT
} image_table_id_t;

#ifdef __cplusplus
}
#endif

#endif /* IMAGE_TABLE_ID_H */
