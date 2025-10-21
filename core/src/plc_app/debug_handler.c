#include "debug_handler.h"
#include "image_tables.h"
#include "utils/log.h"
#include "utils/utils.h"
#include <string.h>

#define MAX_DEBUG_FRAME 4096

#define MB_FC_DEBUG_INFO 0x41
#define MB_FC_DEBUG_SET 0x42
#define MB_FC_DEBUG_GET 0x43
#define MB_FC_DEBUG_GET_LIST 0x44
#define MB_FC_DEBUG_GET_MD5 0x45

#define MB_DEBUG_SUCCESS 0x7E
#define MB_DEBUG_ERROR_OUT_OF_BOUNDS 0x81
#define MB_DEBUG_ERROR_OUT_OF_MEMORY 0x82

#define SAME_ENDIANNESS 0
#define REVERSE_ENDIANNESS 1

#define VARIDX_SIZE 256

static void debugInfo(uint8_t *frame, size_t *frame_len)
{
    uint16_t variableCount = ext_get_var_count();
    *frame_len             = 3;
    frame[0]               = MB_FC_DEBUG_INFO;
    frame[1]               = (uint8_t)(variableCount >> 8);
    frame[2]               = (uint8_t)(variableCount & 0xFF);
}

static void debugSetTrace(uint8_t *frame, size_t *frame_len, uint16_t varidx, uint8_t flag,
                          uint16_t len, void *value)
{
    uint16_t variableCount = ext_get_var_count();
    if (varidx >= variableCount || len > (MAX_DEBUG_FRAME - 7))
    {
        *frame_len = 2;
        frame[0]   = MB_FC_DEBUG_SET;
        frame[1]   = MB_DEBUG_ERROR_OUT_OF_BOUNDS;
        return;
    }

    ext_set_trace((size_t)varidx, (bool)flag, value);

    *frame_len = 2;
    frame[0]   = MB_FC_DEBUG_SET;
    frame[1]   = MB_DEBUG_SUCCESS;
}

static void debugGetTrace(uint8_t *frame, size_t *frame_len, uint16_t startidx, uint16_t endidx)
{
    uint16_t variableCount = ext_get_var_count();
    if (startidx >= variableCount || endidx >= variableCount || startidx > endidx)
    {
        *frame_len = 2;
        frame[0]   = MB_FC_DEBUG_GET;
        frame[1]   = MB_DEBUG_ERROR_OUT_OF_BOUNDS;
        return;
    }

    uint16_t lastVarIdx  = startidx;
    size_t responseSize  = 0;
    uint8_t *responsePtr = &(frame[10]);

    for (uint16_t varidx = startidx; varidx <= endidx; varidx++)
    {
        size_t varSize = ext_get_var_size(varidx);
        if ((responseSize + 10) + varSize <= MAX_DEBUG_FRAME)
        {
            void *varAddr = ext_get_var_addr(varidx);

            memcpy(responsePtr, varAddr, varSize);

            responsePtr += varSize;
            responseSize += varSize;

            lastVarIdx = varidx;
        }
        else
        {
            break;
        }
    }

    *frame_len = 10 + responseSize;
    frame[0]   = MB_FC_DEBUG_GET;
    frame[1]   = MB_DEBUG_SUCCESS;
    frame[2]   = (uint8_t)(lastVarIdx >> 8);
    frame[3]   = (uint8_t)(lastVarIdx & 0xFF);
    frame[4]   = (uint8_t)((tick__ >> 24) & 0xFF);
    frame[5]   = (uint8_t)((tick__ >> 16) & 0xFF);
    frame[6]   = (uint8_t)((tick__ >> 8) & 0xFF);
    frame[7]   = (uint8_t)(tick__ & 0xFF);
    frame[8]   = (uint8_t)(responseSize >> 8);
    frame[9]   = (uint8_t)(responseSize & 0xFF);
}

static void debugGetTraceList(uint8_t *frame, size_t *frame_len, uint16_t numIndexes,
                              uint8_t *indexArray)
{
    uint16_t response_idx  = 10;
    uint16_t responseSize  = 0;
    uint16_t lastVarIdx    = 0;
    uint16_t variableCount = ext_get_var_count();

    uint16_t varidx_array[VARIDX_SIZE];

    if (numIndexes > VARIDX_SIZE)
    {
        *frame_len = 2;
        frame[0]   = MB_FC_DEBUG_GET_LIST;
        frame[1]   = MB_DEBUG_ERROR_OUT_OF_MEMORY;
        return;
    }

    for (uint16_t i = 0; i < numIndexes; i++)
    {
        varidx_array[i] = (uint16_t)indexArray[i * 2] << 8 | indexArray[i * 2 + 1];
    }

    for (uint16_t i = 0; i < numIndexes; i++)
    {
        if (varidx_array[i] >= variableCount)
        {
            *frame_len = 2;
            frame[0]   = MB_FC_DEBUG_GET_LIST;
            frame[1]   = MB_DEBUG_ERROR_OUT_OF_BOUNDS;
            return;
        }

        size_t varSize = ext_get_var_size(varidx_array[i]);

        if (response_idx + varSize <= MAX_DEBUG_FRAME)
        {
            void *varAddr = ext_get_var_addr(varidx_array[i]);
            memcpy(&frame[response_idx], varAddr, varSize);
            response_idx += varSize;
            responseSize += varSize;

            lastVarIdx = varidx_array[i];
        }
        else
        {
            break;
        }
    }

    *frame_len = response_idx;
    frame[0]   = MB_FC_DEBUG_GET_LIST;
    frame[1]   = MB_DEBUG_SUCCESS;
    frame[2]   = (uint8_t)(lastVarIdx >> 8);
    frame[3]   = (uint8_t)(lastVarIdx & 0xFF);
    frame[4]   = (uint8_t)((tick__ >> 24) & 0xFF);
    frame[5]   = (uint8_t)((tick__ >> 16) & 0xFF);
    frame[6]   = (uint8_t)((tick__ >> 8) & 0xFF);
    frame[7]   = (uint8_t)(tick__ & 0xFF);
    frame[8]   = (uint8_t)(responseSize >> 8);
    frame[9]   = (uint8_t)(responseSize & 0xFF);
}

static void debugGetMd5(uint8_t *frame, size_t *frame_len, void *endianness)
{
    uint16_t endian_check = 0;
    memcpy(&endian_check, endianness, 2);
    if (endian_check == 0xDEAD)
    {
        ext_set_endianness(SAME_ENDIANNESS);
    }
    else if (endian_check == 0xADDE)
    {
        ext_set_endianness(REVERSE_ENDIANNESS);
    }
    else
    {
        *frame_len = 2;
        frame[0]   = MB_FC_DEBUG_GET_MD5;
        frame[1]   = MB_DEBUG_ERROR_OUT_OF_BOUNDS;
        return;
    }

    frame[0] = MB_FC_DEBUG_GET_MD5;
    frame[1] = MB_DEBUG_SUCCESS;

    int md5_len      = 0;
    for (md5_len = 0; ext_plc_program_md5[md5_len] != '\0'; md5_len++)
    {
        frame[md5_len + 2] = ext_plc_program_md5[md5_len];
    }

    *frame_len = md5_len + 2;
}

size_t process_debug_data(uint8_t *data, size_t length)
{
    if (length < 1)
    {
        log_error("Debug data too short");
        return 0;
    }

    uint8_t fcode          = data[0];
    uint16_t field1        = 0;
    uint16_t field2        = 0;
    uint8_t flag           = 0;
    uint16_t len           = 0;
    void *value            = NULL;
    void *endianness_check = NULL;

    if (length >= 3)
    {
        field1 = (uint16_t)data[1] << 8 | (uint16_t)data[2];
    }
    if (length >= 5)
    {
        field2 = (uint16_t)data[3] << 8 | (uint16_t)data[4];
    }
    if (length >= 4)
    {
        flag = data[3];
    }
    if (length >= 6)
    {
        len = (uint16_t)data[4] << 8 | (uint16_t)data[5];
    }
    if (length >= 7)
    {
        value = &data[6];
    }
    if (length >= 2)
    {
        endianness_check = &data[1];
    }

    size_t response_len = 0;

    switch (fcode)
    {
    case MB_FC_DEBUG_INFO:
        debugInfo(data, &response_len);
        break;

    case MB_FC_DEBUG_GET:
        debugGetTrace(data, &response_len, field1, field2);
        break;

    case MB_FC_DEBUG_GET_LIST:
        debugGetTraceList(data, &response_len, field1, &data[3]);
        break;

    case MB_FC_DEBUG_SET:
        debugSetTrace(data, &response_len, field1, flag, len, value);
        break;

    case MB_FC_DEBUG_GET_MD5:
        debugGetMd5(data, &response_len, endianness_check);
        break;

    default:
        log_error("Unknown debug function code: 0x%02X", fcode);
        return 0;
    }

    log_debug("Processed debug function 0x%02X, response length: %zu", fcode, response_len);
    return response_len;
}
