// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file ethercat_iomap.c
 * @brief Mapping file parser, layout join and per-cycle copies for the EtherCAT client.
 */

#include "ethercat_iomap.h"

#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Journal buffer-type ids for the INPUT image; must match journal_buffer_type_t. */
#define JOURNAL_BOOL_INPUT 0
#define JOURNAL_BYTE_INPUT 3
#define JOURNAL_INT_INPUT 5
#define JOURNAL_DINT_INPUT 8
#define JOURNAL_LINT_INPUT 11

/* --- IEC location parser ---------------------------------------------------------------- */

int ecat_io_parse_iec_location(const char *loc_str, iec_location_t *loc)
{
    if (!loc_str || !loc)
        return -1;

    const char *p = loc_str;
    if (*p != '%')
        return -1;
    p++;

    switch (toupper((unsigned char)*p)) {
    case 'I': loc->direction = IEC_DIR_INPUT; break;
    case 'Q': loc->direction = IEC_DIR_OUTPUT; break;
    default: return -1;
    }
    p++;

    switch (toupper((unsigned char)*p)) {
    case 'X': loc->size = IEC_SIZE_BIT; break;
    case 'B': loc->size = IEC_SIZE_BYTE; break;
    case 'W': loc->size = IEC_SIZE_WORD; break;
    case 'D': loc->size = IEC_SIZE_DWORD; break;
    case 'L': loc->size = IEC_SIZE_LWORD; break;
    default: return -1;
    }
    p++;

    if (!isdigit((unsigned char)*p))
        return -1;
    char *endptr = NULL;
    errno = 0;
    long byte_val = strtol(p, &endptr, 10);
    if (endptr == p || errno == ERANGE || byte_val < 0 || byte_val > ECAT_IOMAP_MAX_BYTE_INDEX)
        return -1;
    loc->byte_index = (int)byte_val;
    p = endptr;

    loc->bit_index = -1;
    if (*p == '.') {
        p++;
        if (!isdigit((unsigned char)*p))
            return -1;
        long bit_val = strtol(p, &endptr, 10);
        if (endptr == p || bit_val < 0 || bit_val > 7)
            return -1;
        if (loc->size != IEC_SIZE_BIT)
            return -1;
        loc->bit_index = (int)bit_val;
        p = endptr;
    } else if (loc->size == IEC_SIZE_BIT) {
        loc->bit_index = 0;
    }

    return *p == '\0' ? 0 : -1;
}

static int iec_size_bits(iec_size_t size)
{
    switch (size) {
    case IEC_SIZE_BIT: return 1;
    case IEC_SIZE_BYTE: return 8;
    case IEC_SIZE_WORD: return 16;
    case IEC_SIZE_DWORD: return 32;
    case IEC_SIZE_LWORD: return 64;
    }
    return 0;
}

/* --- mapping file ------------------------------------------------------------------------ */

static char *read_file(const char *path, char *err, size_t err_size)
{
    FILE *fp = fopen(path, "rb");
    if (fp == NULL) {
        snprintf(err, err_size, "cannot open %s: %s", path, strerror(errno));
        return NULL;
    }
    fseek(fp, 0, SEEK_END);
    long size = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (size < 0 || size > 16 * 1024 * 1024) {
        fclose(fp);
        snprintf(err, err_size, "%s: unreadable or too large", path);
        return NULL;
    }
    char *text = malloc((size_t)size + 1);
    if (text == NULL) {
        fclose(fp);
        snprintf(err, err_size, "out of memory");
        return NULL;
    }
    size_t n = fread(text, 1, (size_t)size, fp);
    fclose(fp);
    text[n] = '\0';
    return text;
}

static int parse_hex16(const cJSON *item, uint16_t *out)
{
    if (cJSON_IsNumber(item)) {
        if (item->valuedouble < 0 || item->valuedouble > 0xFFFF)
            return -1;
        *out = (uint16_t)item->valueint;
        return 0;
    }
    if (!cJSON_IsString(item))
        return -1;
    char *end = NULL;
    unsigned long v = strtoul(item->valuestring, &end, 0);
    if (end == item->valuestring || *end != '\0' || v > 0xFFFF)
        return -1;
    *out = (uint16_t)v;
    return 0;
}

int ecat_iomap_load(const char *path, ecat_iomap_t *map, char *err, size_t err_size)
{
    memset(map, 0, sizeof(*map));
    char *text = read_file(path, err, err_size);
    if (text == NULL)
        return -1;

    cJSON *root = cJSON_Parse(text);
    free(text);
    if (root == NULL) {
        snprintf(err, err_size, "%s is not valid JSON", path);
        return -1;
    }

    int rc = -1;
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(root, "version");
    const cJSON *masters = cJSON_GetObjectItemCaseSensitive(root, "masters");
    if (!cJSON_IsNumber(version) || version->valueint != 1) {
        snprintf(err, err_size, "%s: unsupported mapping version (expected 1)", path);
        goto done;
    }
    if (!cJSON_IsArray(masters)) {
        snprintf(err, err_size, "%s: missing 'masters' array", path);
        goto done;
    }

    const cJSON *m;
    cJSON_ArrayForEach(m, masters)
    {
        if (map->master_count >= ECAT_IOMAP_MAX_MASTERS) {
            snprintf(err, err_size, "%s: more than %d masters", path, ECAT_IOMAP_MAX_MASTERS);
            goto done;
        }
        ecat_iomap_master_t *mm = &map->masters[map->master_count++];
        const cJSON *name = cJSON_GetObjectItemCaseSensitive(m, "name");
        const cJSON *entries = cJSON_GetObjectItemCaseSensitive(m, "entries");
        if (!cJSON_IsString(name) || !cJSON_IsArray(entries)) {
            snprintf(err, err_size, "%s: each master needs 'name' and 'entries'", path);
            goto done;
        }
        for (int k = 0; k < map->master_count - 1; k++) {
            if (strcmp(map->masters[k].name, name->valuestring) == 0) {
                snprintf(err, err_size, "%s: master name '%s' is used twice", path,
                         name->valuestring);
                goto done;
            }
        }
        snprintf(mm->name, sizeof(mm->name), "%s", name->valuestring);

        const cJSON *e;
        cJSON_ArrayForEach(e, entries)
        {
            if (mm->entry_count >= ECAT_IOMAP_MAX_ENTRIES) {
                snprintf(err, err_size, "%s: master '%s' has more than %d entries", path,
                         mm->name, ECAT_IOMAP_MAX_ENTRIES);
                goto done;
            }
            ecat_iomap_entry_t *me = &mm->entries[mm->entry_count];
            const cJSON *slave = cJSON_GetObjectItemCaseSensitive(e, "slave");
            const cJSON *index = cJSON_GetObjectItemCaseSensitive(e, "index");
            const cJSON *sub = cJSON_GetObjectItemCaseSensitive(e, "subindex");
            const cJSON *loc = cJSON_GetObjectItemCaseSensitive(e, "iec_location");
            if (!cJSON_IsNumber(slave) || slave->valueint < 1 || parse_hex16(index, &me->index) ||
                !cJSON_IsNumber(sub) || sub->valueint < 0 || sub->valueint > 255 ||
                !cJSON_IsString(loc) || strlen(loc->valuestring) >= sizeof(me->iec_location)) {
                snprintf(err, err_size, "%s: master '%s' entry %d is malformed", path, mm->name,
                         mm->entry_count);
                goto done;
            }
            me->slave = slave->valueint;
            me->subindex = (uint8_t)sub->valueint;
            snprintf(me->iec_location, sizeof(me->iec_location), "%s", loc->valuestring);
            if (ecat_io_parse_iec_location(me->iec_location, &me->loc) != 0) {
                snprintf(err, err_size, "%s: invalid IEC location '%s' (slave %d, 0x%04X:%u)",
                         path, me->iec_location, me->slave, me->index, me->subindex);
                goto done;
            }
            mm->entry_count++;
        }
    }
    rc = 0;

done:
    cJSON_Delete(root);
    return rc;
}

/* --- binding ------------------------------------------------------------------------------ */

static void *resolve_plc_ptr(const iec_location_t *loc, plugin_runtime_args_t *args)
{
    int i = loc->byte_index;
    if (loc->direction == IEC_DIR_INPUT) {
        switch (loc->size) {
        case IEC_SIZE_BIT:
            return args->bool_input ? args->bool_input[i][loc->bit_index] : NULL;
        case IEC_SIZE_BYTE: return args->byte_input ? args->byte_input[i] : NULL;
        case IEC_SIZE_WORD: return args->int_input ? args->int_input[i] : NULL;
        case IEC_SIZE_DWORD: return args->dint_input ? args->dint_input[i] : NULL;
        case IEC_SIZE_LWORD: return args->lint_input ? args->lint_input[i] : NULL;
        }
    } else {
        switch (loc->size) {
        case IEC_SIZE_BIT:
            return args->bool_output ? args->bool_output[i][loc->bit_index] : NULL;
        case IEC_SIZE_BYTE: return args->byte_output ? args->byte_output[i] : NULL;
        case IEC_SIZE_WORD: return args->int_output ? args->int_output[i] : NULL;
        case IEC_SIZE_DWORD: return args->dint_output ? args->dint_output[i] : NULL;
        case IEC_SIZE_LWORD: return args->lint_output ? args->lint_output[i] : NULL;
        }
    }
    return NULL;
}

static const cJSON *find_layout_master(const cJSON *layout, const char *name)
{
    const cJSON *masters = cJSON_GetObjectItemCaseSensitive(layout, "masters");
    const cJSON *m;
    cJSON_ArrayForEach(m, masters)
    {
        const cJSON *n = cJSON_GetObjectItemCaseSensitive(m, "name");
        if (cJSON_IsString(n) && strcmp(n->valuestring, name) == 0)
            return m;
    }
    return NULL;
}

static const cJSON *find_layout_entry(const cJSON *lm, const ecat_iomap_entry_t *me)
{
    const cJSON *entries = cJSON_GetObjectItemCaseSensitive(lm, "entries");
    const cJSON *e;
    cJSON_ArrayForEach(e, entries)
    {
        const cJSON *slave = cJSON_GetObjectItemCaseSensitive(e, "slave");
        const cJSON *index = cJSON_GetObjectItemCaseSensitive(e, "index");
        const cJSON *sub = cJSON_GetObjectItemCaseSensitive(e, "subindex");
        uint16_t idx = 0;
        if (cJSON_IsNumber(slave) && slave->valueint == me->slave && parse_hex16(index, &idx) == 0 &&
            idx == me->index && cJSON_IsNumber(sub) && sub->valueint == me->subindex)
            return e;
    }
    return NULL;
}

int ecat_iomap_bind(const ecat_iomap_t *map, const cJSON *layout, plugin_runtime_args_t *args,
                    ecat_bound_map_t *out, char *err, size_t err_size)
{
    memset(out, 0, sizeof(*out));

    for (int mi = 0; mi < map->master_count; mi++) {
        const ecat_iomap_master_t *mm = &map->masters[mi];
        const cJSON *lm = find_layout_master(layout, mm->name);
        if (lm == NULL) {
            snprintf(err, err_size, "master '%s' is mapped but not running in EtherDOG", mm->name);
            return -1;
        }
        const cJSON *idx = cJSON_GetObjectItemCaseSensitive(lm, "index");
        const cJSON *ready = cJSON_GetObjectItemCaseSensitive(lm, "ready");
        if (!cJSON_IsNumber(idx) || idx->valueint < 0 || idx->valueint >= ECAT_IOMAP_MAX_MASTERS) {
            snprintf(err, err_size, "master '%s': invalid index in layout", mm->name);
            return -1;
        }
        if (!cJSON_IsTrue(ready)) {
            snprintf(out->not_ready[out->not_ready_count++], ECAT_IOMAP_NAME_LEN, "%s", mm->name);
            continue;
        }

        ecat_bound_master_t *bm = &out->masters[idx->valueint];
        if (bm->active) {
            snprintf(err, err_size, "master '%s': layout index %d is bound twice", mm->name,
                     idx->valueint);
            return -1;
        }
        bm->active = true;
        bm->output_bytes =
            (uint32_t)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(lm, "output_bytes"));
        bm->input_bytes =
            (uint32_t)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(lm, "input_bytes"));

        for (int ei = 0; ei < mm->entry_count; ei++) {
            const ecat_iomap_entry_t *me = &mm->entries[ei];
            const cJSON *le = find_layout_entry(lm, me);
            if (le == NULL) {
                snprintf(err, err_size,
                         "%s (master '%s', slave %d, 0x%04X:%u) has no matching process data entry",
                         me->iec_location, mm->name, me->slave, me->index, me->subindex);
                return -1;
            }
            const cJSON *dir = cJSON_GetObjectItemCaseSensitive(le, "direction");
            int bit_offset = (int)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(le, "bit_offset"));
            int bit_length = (int)cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(le, "bit_length"));
            bool is_output = cJSON_IsString(dir) && strcmp(dir->valuestring, "output") == 0;
            if (is_output != (me->loc.direction == IEC_DIR_OUTPUT)) {
                snprintf(err, err_size, "%s (slave %d, 0x%04X:%u) is %s data but mapped to %s",
                         me->iec_location, me->slave, me->index, me->subindex,
                         is_output ? "output" : "input", me->loc.direction == IEC_DIR_OUTPUT ? "%Q" : "%I");
                return -1;
            }
            if (bit_length != iec_size_bits(me->loc.size)) {
                snprintf(err, err_size, "%s (slave %d, 0x%04X:%u) is %d bits wide but the location holds %d",
                         me->iec_location, me->slave, me->index, me->subindex, bit_length,
                         iec_size_bits(me->loc.size));
                return -1;
            }
            uint32_t region_bits = 8u * (is_output ? bm->output_bytes : bm->input_bytes);
            if (bit_offset < 0 || (uint32_t)(bit_offset + bit_length) > region_bits) {
                snprintf(err, err_size, "%s: layout offset out of range", me->iec_location);
                return -1;
            }
            if (me->loc.byte_index < 0 || me->loc.byte_index >= args->buffer_size) {
                snprintf(err, err_size, "%s exceeds the image table size (%d)", me->iec_location,
                         args->buffer_size);
                return -1;
            }

            ecat_xfer_t x = {
                .bit_offset = (uint32_t)bit_offset,
                .bit_length = (uint8_t)bit_length,
                .size = me->loc.size,
                .plc_ptr = resolve_plc_ptr(&me->loc, args),
                .journal_index = me->loc.byte_index,
                .journal_bit = me->loc.size == IEC_SIZE_BIT ? me->loc.bit_index : 0,
            };
            int *count = is_output ? &bm->output_count : &bm->input_count;
            if (*count >= ECAT_IOMAP_MAX_ENTRIES) {
                snprintf(err, err_size, "master '%s' has more than %d %s entries", mm->name,
                         ECAT_IOMAP_MAX_ENTRIES, is_output ? "output" : "input");
                return -1;
            }
            if (is_output) {
                if (x.plc_ptr == NULL)
                    continue; /* location not declared in the program */
                bm->outputs[(*count)++] = x;
            } else {
                bm->inputs[(*count)++] = x;
            }
        }
    }
    if (map->master_count > 0 && out->not_ready_count == map->master_count) {
        snprintf(err, err_size, "no mapped master is operational (first: '%s')", out->not_ready[0]);
        return -1;
    }
    return 0;
}

/* --- per-cycle copies ----------------------------------------------------------------------- */

static uint64_t read_field(const uint8_t *payload, uint32_t bit_offset, int bits)
{
    if ((bit_offset & 7) == 0 && bits >= 8) {
        uint64_t v = 0;
        const uint8_t *p = payload + bit_offset / 8;
        for (int i = bits / 8 - 1; i >= 0; i--)
            v = (v << 8) | p[i];
        return v;
    }
    uint64_t v = 0;
    for (int i = 0; i < bits; i++) {
        uint32_t b = bit_offset + (uint32_t)i;
        if (payload[b / 8] >> (b % 8) & 1)
            v |= (uint64_t)1 << i;
    }
    return v;
}

static void write_field(uint8_t *payload, uint32_t bit_offset, int bits, uint64_t v)
{
    if ((bit_offset & 7) == 0 && bits >= 8) {
        uint8_t *p = payload + bit_offset / 8;
        for (int i = 0; i < bits / 8; i++)
            p[i] = (uint8_t)(v >> (8 * i));
        return;
    }
    for (int i = 0; i < bits; i++) {
        uint32_t b = bit_offset + (uint32_t)i;
        uint8_t mask = (uint8_t)(1u << (b % 8));
        if (v >> i & 1)
            payload[b / 8] |= mask;
        else
            payload[b / 8] &= (uint8_t)~mask;
    }
}

void ecat_iomap_publish_inputs(const ecat_bound_master_t *m, const uint8_t *payload, size_t len,
                               plugin_runtime_args_t *args)
{
    if (len < m->input_bytes)
        return;
    for (int i = 0; i < m->input_count; i++) {
        const ecat_xfer_t *x = &m->inputs[i];
        uint64_t v = read_field(payload, x->bit_offset, x->bit_length);
        switch (x->size) {
        case IEC_SIZE_BIT:
            args->journal_write_bool(JOURNAL_BOOL_INPUT, x->journal_index, x->journal_bit, (int)v);
            break;
        case IEC_SIZE_BYTE:
            args->journal_write_byte(JOURNAL_BYTE_INPUT, x->journal_index, (uint8_t)v);
            break;
        case IEC_SIZE_WORD:
            args->journal_write_int(JOURNAL_INT_INPUT, x->journal_index, (uint16_t)v);
            break;
        case IEC_SIZE_DWORD:
            args->journal_write_dint(JOURNAL_DINT_INPUT, x->journal_index, (uint32_t)v);
            break;
        case IEC_SIZE_LWORD:
            args->journal_write_lint(JOURNAL_LINT_INPUT, x->journal_index, v);
            break;
        }
    }
}

void ecat_iomap_collect_outputs(const ecat_bound_master_t *m, uint8_t *payload, size_t len)
{
    memset(payload, 0, len);
    if (len < m->output_bytes)
        return;
    for (int i = 0; i < m->output_count; i++) {
        const ecat_xfer_t *x = &m->outputs[i];
        uint64_t v = 0;
        switch (x->size) {
        case IEC_SIZE_BIT: v = *(const uint8_t *)x->plc_ptr ? 1 : 0; break;
        case IEC_SIZE_BYTE: v = *(const uint8_t *)x->plc_ptr; break;
        case IEC_SIZE_WORD: v = *(const uint16_t *)x->plc_ptr; break;
        case IEC_SIZE_DWORD: v = *(const uint32_t *)x->plc_ptr; break;
        case IEC_SIZE_LWORD: v = *(const uint64_t *)x->plc_ptr; break;
        }
        write_field(payload, x->bit_offset, x->bit_length, v);
    }
}
