/* A hand-written HMI manifest for the host tests (hmi_access.h): four tags and three screens.
 * Screen 0 "Control" reads every tag and writes lamp, speed and label (admin, engineer, operator);
 * screen 1 "Status" only reads temp (every role); screen 2 "Setup" reads and writes speed (admin).
 */
#include "hmi_access.h"

static const uint16_t control_reads[]  = {0, 1, 2, 3};
static const uint16_t control_writes[] = {0, 1, 2};
static const uint16_t status_reads[]   = {3};
static const uint16_t setup_ids[]      = {1};

const hmi_access_tag_t hmi_access_tags[] = {
    {0u, 1u, HMI_ACCESS_BOOL, 0, "CONFIG0.RES0.INSTANCE0.MAIN.LAMP"},
    {0u, 2u, HMI_ACCESS_INT, 0, "CONFIG0.RES0.INSTANCE0.MAIN.SPEED"},
    {0u, 3u, HMI_ACCESS_STRING, 20, "CONFIG0.RES0.INSTANCE0.MAIN.LABEL"},
    {0u, 4u, HMI_ACCESS_REAL, 0, "CONFIG0.RES0.INSTANCE0.MAIN.TEMP"},
};
const int hmi_access_tag_count             = 4;
const hmi_access_page_t hmi_access_pages[] = {
    {control_reads, 4, control_writes, 3, 0x7},
    {status_reads, 1, status_reads, 0, 0xF},
    {setup_ids, 1, setup_ids, 1, 0x1},
};
const int hmi_access_page_count     = 3;
const char hmi_access_plc_md5[]     = "0123456789abcdef0123456789abcdef";
const uint32_t hmi_access_period_ms = 20u;
