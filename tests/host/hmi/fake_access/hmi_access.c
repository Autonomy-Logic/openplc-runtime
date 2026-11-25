/* A hand-written HMI manifest for the host tests (hmi_access.h): five tags and three screens.
 * Screen 0 "Control" reads every tag, writes lamp, speed (0 to 100, as a slider) and label (at most
 * 10 bytes) whole and sets bit 2 of flags (admin, engineer, operator); screen 1 "Status" only reads
 * temp (every role); screen 2 "Setup" reads and writes speed (admin). */
#include "hmi_access.h"

static const uint16_t control_reads[]            = {0, 1, 2, 3, 4};
static const hmi_access_write_t control_writes[] = {{0, 1, 0x0ULL, 0, 0.0, 0.0},
                                                    {1, 1, 0x0ULL, 1, 0.0, 100.0},
                                                    {2, 1, 0x0ULL, 1, 0.0, 10.0},
                                                    {4, 0, 0x4ULL, 0, 0.0, 0.0}};
static const uint16_t status_reads[]             = {3};
static const hmi_access_write_t no_writes[]      = {{0, 0, 0x0ULL, 0, 0.0, 0.0}};
static const uint16_t setup_reads[]              = {1};
static const hmi_access_write_t setup_writes[]   = {{1, 1, 0x0ULL, 0, 0.0, 0.0}};

const hmi_access_tag_t hmi_access_tags[] = {
    {0u, 1u, HMI_ACCESS_BOOL, 0, "CONFIG0.RES0.INSTANCE0.MAIN.LAMP"},
    {0u, 2u, HMI_ACCESS_INT, 0, "CONFIG0.RES0.INSTANCE0.MAIN.SPEED"},
    {0u, 3u, HMI_ACCESS_STRING, 20, "CONFIG0.RES0.INSTANCE0.MAIN.LABEL"},
    {0u, 4u, HMI_ACCESS_REAL, 0, "CONFIG0.RES0.INSTANCE0.MAIN.TEMP"},
    {0u, 5u, HMI_ACCESS_DINT, 0, "CONFIG0.RES0.INSTANCE0.MAIN.FLAGS"},
};
const int hmi_access_tag_count             = 5;
const hmi_access_page_t hmi_access_pages[] = {
    {control_reads, 5, control_writes, 4, 0x7},
    {status_reads, 1, no_writes, 0, 0xF},
    {setup_reads, 1, setup_writes, 1, 0x1},
};
const int hmi_access_page_count     = 3;
const char hmi_access_plc_md5[]     = "0123456789abcdef0123456789abcdef";
const uint32_t hmi_access_period_ms = 20u;
