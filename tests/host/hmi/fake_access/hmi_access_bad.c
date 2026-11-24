/* Manifests the host must refuse, one per HMI_BAD value (1-14), and the valid base (0): built with
 * test_hmi_bad_manifest.cpp, which expects out of date and no variable access for every bad one. */
#include "hmi_access.h"

#ifndef HMI_BAD
#define HMI_BAD 0
#endif

#if HMI_BAD == 1
static const uint16_t reads[] = {1, 0};
#elif HMI_BAD == 2
static const uint16_t reads[] = {0, 0};
#elif HMI_BAD == 3
static const uint16_t reads[] = {0, 9};
#else
static const uint16_t reads[] = {0, 1};
#endif

#if HMI_BAD == 4
static const hmi_access_write_t writes[] = {{9, 1, 0x0ULL, 0, 0.0, 0.0}};
#elif HMI_BAD == 5
static const hmi_access_write_t writes[] = {{1, 1, 0x0ULL, 0, 0.0, 0.0},
                                            {0, 1, 0x0ULL, 0, 0.0, 0.0}};
#elif HMI_BAD == 9
static const hmi_access_write_t writes[] = {{1, 1, 0x0ULL, 1, 5.0, 1.0}};
#elif HMI_BAD == 10
static const hmi_access_write_t writes[] = {{1, 1, 0x0ULL, 1, __builtin_nan(""), 1.0}};
#elif HMI_BAD == 11
static const hmi_access_write_t writes[] = {{2, 0, 0x1ULL, 0, 0.0, 0.0}};
#elif HMI_BAD == 12
static const hmi_access_write_t writes[] = {{1, 0, 0x100000ULL, 0, 0.0, 0.0}};
#elif HMI_BAD == 13
static const hmi_access_write_t writes[] = {{1, 0, 0x0ULL, 0, 0.0, 0.0}};
#elif HMI_BAD == 14
static const hmi_access_write_t writes[] = {{1, 0, 0x1ULL, 1, 0.0, 1.0}};
#else
static const hmi_access_write_t writes[] = {{1, 1, 0x0ULL, 1, 0.0, 100.0}};
#endif

const hmi_access_tag_t hmi_access_tags[] = {
    {0u, 1u, HMI_ACCESS_BOOL, 0, "CONFIG0.RES0.INSTANCE0.MAIN.LAMP"},
    {0u, 2u, HMI_ACCESS_INT, 0, "CONFIG0.RES0.INSTANCE0.MAIN.SPEED"},
#if HMI_BAD == 6
    {0u, 4u, 99, 0, "CONFIG0.RES0.INSTANCE0.MAIN.TEMP"},
#elif HMI_BAD == 7
    {0u, 3u, HMI_ACCESS_STRING, 2000, "CONFIG0.RES0.INSTANCE0.MAIN.LABEL"},
#else
    {0u, 4u, HMI_ACCESS_REAL, 0, "CONFIG0.RES0.INSTANCE0.MAIN.TEMP"},
#endif
};
const int hmi_access_tag_count             = 3;
const hmi_access_page_t hmi_access_pages[] = {
    {reads, 2, writes, sizeof writes / sizeof writes[0], 0xF}};
#if HMI_BAD == 8
const int hmi_access_page_count = 0;
#else
const int hmi_access_page_count = 1;
#endif
const char hmi_access_plc_md5[]     = "0123456789abcdef0123456789abcdef";
const uint32_t hmi_access_period_ms = 20u;
