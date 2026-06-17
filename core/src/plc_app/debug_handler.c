/*
 * debug_handler.c — STruC++ hierarchical debugger PDU handler.
 *
 * The Modbus-style function codes (0x41-0x45) are kept for wire compatibility
 * with the editor and the Arduino runtime. The payload format uses
 * (array_idx: u8, elem_idx: u16) addressing — the editor's debug-map.json
 * carries the path → (arr, elem) mapping.
 *
 * Mirrors the dispatch logic in resources/sources/StrucppBaremetal/ModbusSlave.cpp
 * from the editor repo. Linux supports larger PDUs than RTU/Arduino; the cap
 * here is the runtime-side MAX_DEBUG_FRAME, not the conservative 1400-byte
 * limit the Arduino sketch uses.
 */

#include <string.h>

#include "debug_handler.h"
#include "debug_write_journal.h"
#include "image_tables.h"
#include "utils/log.h"
#include "utils/utils.h"

#define MAX_DEBUG_FRAME 4096

#define MB_FC_DEBUG_INFO     0x41
#define MB_FC_DEBUG_SET      0x42
#define MB_FC_DEBUG_GET      0x43
#define MB_FC_DEBUG_GET_LIST 0x44
#define MB_FC_DEBUG_GET_MD5  0x45

#define MB_DEBUG_SUCCESS              0x7E
#define MB_DEBUG_ERROR_OUT_OF_BOUNDS  0x81
#define MB_DEBUG_ERROR_OUT_OF_MEMORY  0x82
#define MB_DEBUG_ERROR_NOT_LOADED     0x83

/* Each FC 0x44 request entry is 3 bytes (arr + elem_hi + elem_lo). With
 * MAX_DEBUG_FRAME = 4096, 1024 entries fit comfortably plus the response
 * body. */
#define VARIDX_SIZE 1024

/* MD5 hex strings are exactly 32 chars + null. Bounding the read both by
 * the expected length and pos<MAX guards against a malformed .so that
 * exports a non-terminated strucpp_program_md5. */
#define MD5_HEX_LEN 32

static inline bool debug_symbols_ready(void)
{
    return ext_strucpp_debug_array_count != NULL &&
           ext_strucpp_debug_elem_count  != NULL &&
           ext_strucpp_debug_size        != NULL &&
           ext_strucpp_debug_set         != NULL &&
           ext_strucpp_debug_read        != NULL;
}

/* Defense-in-depth bounds check on the array index that arrived over the
 * wire. The editor's STruC++ codegen validates `arr` inside its debug
 * thunks, but a malformed .so could OOB-read its internal table.  Runtime
 * gate first: reject `arr >= array_count` here so the .so only sees
 * indices it claimed to support. */
static inline bool debug_arr_in_range(uint8_t arr)
{
    return arr < ext_strucpp_debug_array_count();
}

static inline void write_u32_be(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)((v >> 24) & 0xFF);
    p[1] = (uint8_t)((v >> 16) & 0xFF);
    p[2] = (uint8_t)((v >>  8) & 0xFF);
    p[3] = (uint8_t)(v & 0xFF);
}

static inline void write_u16_be(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)((v >> 8) & 0xFF);
    p[1] = (uint8_t)(v & 0xFF);
}

static inline uint16_t read_u16_be(const uint8_t *p)
{
    return ((uint16_t)p[0] << 8) | (uint16_t)p[1];
}

static void respond_short(uint8_t *frame, size_t *frame_len, uint8_t fc, uint8_t status)
{
    frame[0]   = fc;
    frame[1]   = status;
    *frame_len = 2;
}

/* FC 0x41 — DEBUG_INFO */
static void debugInfo(uint8_t *frame, size_t *frame_len)
{
    if (!debug_symbols_ready())
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_INFO, MB_DEBUG_ERROR_NOT_LOADED);
        return;
    }

    uint8_t arr_count = ext_strucpp_debug_array_count();
    uint8_t max_arrs  = (uint8_t)((MAX_DEBUG_FRAME - 3) / 2);
    if (arr_count > max_arrs) arr_count = max_arrs;

    frame[0] = MB_FC_DEBUG_INFO;
    frame[1] = arr_count;
    frame[2] = MB_DEBUG_SUCCESS;

    size_t pos = 3;
    for (uint8_t a = 0; a < arr_count; ++a)
    {
        uint16_t c = ext_strucpp_debug_elem_count(a);
        write_u16_be(&frame[pos], c);
        pos += 2;
    }
    *frame_len = pos;
}

/* FC 0x42 — DEBUG_SET (force / unforce a variable) */
static void debugSetTrace(uint8_t *frame, size_t *frame_len, size_t length)
{
    if (!debug_symbols_ready())
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_SET, MB_DEBUG_ERROR_NOT_LOADED);
        return;
    }
    if (length < 7)
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_SET, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }

    uint8_t  arr     = frame[1];
    uint16_t elem    = read_u16_be(&frame[2]);
    uint8_t  force   = frame[4];
    uint16_t val_len = read_u16_be(&frame[5]);
    const uint8_t *val_ptr = (val_len > 0) ? &frame[7] : NULL;

    if (!debug_arr_in_range(arr))
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_SET, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }
    if (val_len > (MAX_DEBUG_FRAME - 7))
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_SET, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }
    if (length < (size_t)(7 + val_len))
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_SET, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }

    /* Do NOT poke the IECVar from this socket thread — that races the IEC task
     * workers (OpenPLC bug #3). Enqueue the force/unforce; the dispatcher
     * applies it at the no-task-running window (race-free, ~1 scan later). The
     * editor polls continuously, so the small latency is invisible. */
    uint8_t op = (force != 0) ? (uint8_t)DBGW_OP_FORCE : (uint8_t)DBGW_OP_UNFORCE;
    const uint8_t *vp = (force != 0) ? val_ptr : NULL;
    uint16_t       vl = (force != 0) ? val_len : 0;
    int rc = runtime_external_write(arr, elem, op, vp, vl);
    uint8_t status = (rc == 0) ? MB_DEBUG_SUCCESS : MB_DEBUG_ERROR_OUT_OF_MEMORY;
    respond_short(frame, frame_len, MB_FC_DEBUG_SET, status);
}

/* FC 0x43 — DEBUG_GET (read a contiguous range from one array) */
static void debugGetTrace(uint8_t *frame, size_t *frame_len, size_t length)
{
    if (!debug_symbols_ready())
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET, MB_DEBUG_ERROR_NOT_LOADED);
        return;
    }
    if (length < 6)
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }

    uint8_t  arr   = frame[1];
    uint16_t start = read_u16_be(&frame[2]);
    uint16_t end   = read_u16_be(&frame[4]);

    if (!debug_arr_in_range(arr))
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }
    uint16_t arr_len = ext_strucpp_debug_elem_count(arr);

    if (arr_len == 0 || start >= arr_len || end >= arr_len || start > end)
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }

    /* Header layout (matches Arduino):
     * [0]   FC
     * [1]   STATUS
     * [2-3] last_elem_idx (u16 BE)
     * [4-7] tick (u32 BE)
     * [8-9] response_size (u16 BE)
     * [10..] data
     */
    const size_t HDR = 10;
    uint16_t last_elem   = start;
    size_t   response_sz = 0;
    uint8_t *write_ptr   = &frame[HDR];

    for (uint16_t e = start; e <= end; ++e)
    {
        uint16_t var_size = ext_strucpp_debug_size(arr, e);
        if (var_size == 0)
        {
            last_elem = e;
            continue;
        }
        if (HDR + response_sz + var_size > MAX_DEBUG_FRAME) break;

        uint16_t n = ext_strucpp_debug_read(arr, e, write_ptr);
        if (n == 0)
        {
            last_elem = e;
            continue;
        }
        write_ptr   += n;
        response_sz += n;
        last_elem    = e;
    }

    frame[0] = MB_FC_DEBUG_GET;
    frame[1] = MB_DEBUG_SUCCESS;
    write_u16_be(&frame[2], last_elem);
    write_u32_be(&frame[4], (uint32_t)scan_counter);
    write_u16_be(&frame[8], (uint16_t)response_sz);
    *frame_len = HDR + response_sz;
}

/* FC 0x44 — DEBUG_GET_LIST (cross-array batch read).
 *
 * The request and response both live in `frame[]`. Once we start writing
 * the response body, we'd clobber later request entries. Snapshot the
 * request first — same trick the Arduino sketch uses. */
static void debugGetTraceList(uint8_t *frame, size_t *frame_len, size_t length)
{
    if (!debug_symbols_ready())
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET_LIST, MB_DEBUG_ERROR_NOT_LOADED);
        return;
    }
    if (length < 3)
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET_LIST, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }

    uint16_t num_indexes = read_u16_be(&frame[1]);
    if (num_indexes == 0)
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET_LIST, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }
    if (num_indexes > VARIDX_SIZE)
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET_LIST, MB_DEBUG_ERROR_OUT_OF_MEMORY);
        return;
    }
    if (length < (size_t)(3 + (size_t)num_indexes * 3))
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET_LIST, MB_DEBUG_ERROR_OUT_OF_BOUNDS);
        return;
    }

    uint8_t local_index[VARIDX_SIZE * 3];
    memcpy(local_index, &frame[3], (size_t)num_indexes * 3);

    const size_t HDR = 10;
    uint16_t last_req_idx = 0;
    size_t   response_sz  = 0;
    uint8_t *write_ptr    = &frame[HDR];

    for (uint16_t i = 0; i < num_indexes; ++i)
    {
        uint8_t  arr  = local_index[i * 3 + 0];
        uint16_t elem = ((uint16_t)local_index[i * 3 + 1] << 8) | local_index[i * 3 + 2];

        if (!debug_arr_in_range(arr))
        {
            last_req_idx = i;
            continue;
        }

        uint16_t var_size = ext_strucpp_debug_size(arr, elem);
        if (var_size == 0)
        {
            last_req_idx = i;
            continue;
        }
        if (HDR + response_sz + var_size > MAX_DEBUG_FRAME) break;

        uint16_t n = ext_strucpp_debug_read(arr, elem, write_ptr);
        if (n == 0)
        {
            last_req_idx = i;
            continue;
        }
        write_ptr    += n;
        response_sz  += n;
        last_req_idx  = i;
    }

    frame[0] = MB_FC_DEBUG_GET_LIST;
    frame[1] = MB_DEBUG_SUCCESS;
    write_u16_be(&frame[2], last_req_idx);
    write_u32_be(&frame[4], (uint32_t)scan_counter);
    write_u16_be(&frame[8], (uint16_t)response_sz);
    *frame_len = HDR + response_sz;
}

/* FC 0x45 — DEBUG_GET_MD5
 *
 * The trailer carries a runtime-driven endianness sentinel, not an echo of
 * what the editor sent.  The variable-data path is pure memcpy on both
 * sides (the strucpp dispatcher does no byte-order adaptation), so wire
 * bytes for force / read are always in target-native order.  To let the
 * editor know what "native" means here, this handler writes the literal
 * value 0xDEAD via a native uint16_t store; the two bytes that land in the
 * frame reflect the target's byte order:
 *
 *     LE target  →  trailer = [0xAD, 0xDE]
 *     BE target  →  trailer = [0xDE, 0xAD]
 *
 * The editor uses that to decide whether to byte-swap variable data at its
 * end.  The probe bytes in the request are ignored — the trailer is a
 * sentinel, not an echo.
 */
static void debugGetMd5(uint8_t *frame, size_t *frame_len, size_t length)
{
    if (length < 3 || ext_strucpp_program_md5 == NULL)
    {
        respond_short(frame, frame_len, MB_FC_DEBUG_GET_MD5, MB_DEBUG_ERROR_NOT_LOADED);
        return;
    }

    frame[0] = MB_FC_DEBUG_GET_MD5;
    frame[1] = MB_DEBUG_SUCCESS;

    size_t pos = 2;
    for (size_t i = 0;
         i < MD5_HEX_LEN && ext_strucpp_program_md5[i] != '\0' && pos < MAX_DEBUG_FRAME - 2;
         ++i)
    {
        frame[pos++] = (uint8_t)ext_strucpp_program_md5[i];
    }
    if (pos + 2 <= MAX_DEBUG_FRAME)
    {
        /* Native-order store of the endianness sentinel — the resulting
         * bytes reveal the target's byte order to the editor. */
        *(uint16_t *)&frame[pos] = 0xDEAD;
        pos += 2;
    }
    *frame_len = pos;
}

size_t process_debug_data(uint8_t *data, size_t length)
{
    if (length < 1)
    {
        log_error("[debug] frame too short");
        return 0;
    }

    size_t  response_len = 0;
    uint8_t fcode        = data[0];

    switch (fcode)
    {
    case MB_FC_DEBUG_INFO:     debugInfo(data, &response_len);                  break;
    case MB_FC_DEBUG_SET:      debugSetTrace(data, &response_len, length);      break;
    case MB_FC_DEBUG_GET:      debugGetTrace(data, &response_len, length);      break;
    case MB_FC_DEBUG_GET_LIST: debugGetTraceList(data, &response_len, length);  break;
    case MB_FC_DEBUG_GET_MD5:  debugGetMd5(data, &response_len, length);        break;
    default:
        log_error("[debug] unknown function code 0x%02X", fcode);
        return 0;
    }

    log_debug("[debug] FC 0x%02X processed, response_len=%zu", fcode, response_len);
    return response_len;
}
