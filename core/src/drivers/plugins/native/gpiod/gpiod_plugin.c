#include <gpiod.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* OpenPLC plugin includes */
#include "../plugin_logger.h"
#include "iec_types.h"
#include "plugin_types.h"

/* Logger */
static plugin_logger_t g_logger;

/* Runtime args */
static plugin_runtime_args_t g_args; // Plugin-owned copy

/* Plugin state */
static int plugin_initialized = 0;
static int plugin_running     = 0;

/* Fixed-size arrays */
#define BUFFER_SIZE 1024
#define BITS_PER_BYTE 8

static struct gpiod_line_request *inputs[BUFFER_SIZE][BITS_PER_BYTE]  = {0};
static struct gpiod_line_request *outputs[BUFFER_SIZE][BITS_PER_BYTE] = {0};

static int io_initialized = 0;

/* ----------------- IEC parser ----------------- */
static int parse_iec(const char *s, uint16_t *byte, uint8_t *bit, int *is_input)
{
    char io, type;
    int b, bt;
    if (sscanf(s, "%%%c%c%d.%d", &io, &type, &b, &bt) != 4)
        return -1;
    if (type != 'X' || bt < 0 || bt > 7)
        return -1;
    *byte     = (uint16_t)b;
    *bit      = (uint8_t)bt;
    *is_input = (io == 'I') ? 1 : 0;
    return 0;
}

static int resolve_line_offset(const char *chip_name, const char *line_name, unsigned *offset_out)
{
    struct gpiod_chip *chip = gpiod_chip_open(chip_name);
    if (!chip)
        return -1;

    struct gpiod_chip_info *chip_info = gpiod_chip_get_info(chip);
    if (!chip_info)
    {
        gpiod_chip_close(chip);
        return -1;
    }

    unsigned nlines = gpiod_chip_info_get_num_lines(chip_info);
    gpiod_chip_info_free(chip_info);

    struct gpiod_line_info *info;
    for (unsigned i = 0; i < nlines; i++)
    {
        info = gpiod_chip_get_line_info(chip, i);
        if (!info)
            continue;
        const char *name = gpiod_line_info_get_name(info);
        if (name && strcmp(name, line_name) == 0)
        {
            *offset_out = i;
            gpiod_line_info_free(info);
            gpiod_chip_close(chip);
            return 0;
        }
        gpiod_line_info_free(info);
    }
    gpiod_chip_close(chip);
    return -1;
}

static int parse_line_identifier(const char *s, const char *chip, unsigned *offset)
{
    char *end;
    unsigned long v = strtoul(s, &end, 10);
    if (*end == '\0')
    {
        *offset = (unsigned)v;
        return 0;
    }
    return resolve_line_offset(chip, s, offset);
}

static int init_io_from_csv(const char *filename)
{
    FILE *fp = fopen(filename, "r");
    if (!fp)
    {
        plugin_logger_error(&g_logger, "Failed to open CSV: %s", filename);
        return -1;
    }

    char line[256];
    fgets(line, sizeof(line), fp); // skip header

    for (int b = 0; b < BUFFER_SIZE; b++)
        for (int bit = 0; bit < BITS_PER_BYTE; bit++)
            inputs[b][bit] = outputs[b][bit] = NULL;

    while (fgets(line, sizeof(line), fp))
    {
        /* Strip trailing CR/LF */
        line[strcspn(line, "\r")] = '\0';
        line[strcspn(line, "\n")] = '\0';

        char *iec  = strtok(line, ",");
        char *chip = strtok(NULL, ",");
        char *lin  = strtok(NULL, ",");
        if (!iec || !chip || !lin)
            continue;

        uint16_t byte;
        uint8_t bit;
        int is_input;
        if (parse_iec(iec, &byte, &bit, &is_input) != 0)
            continue;
        if (byte >= BUFFER_SIZE || bit >= BITS_PER_BYTE)
            continue;

        unsigned offset;
        if (parse_line_identifier(lin, chip, &offset) != 0)
        {
            plugin_logger_warn(&g_logger, "Line %s:%s not found", chip, lin);
            continue;
        }

        struct gpiod_chip *gchip = gpiod_chip_open(chip);
        if (!gchip)
        {
            plugin_logger_error(&g_logger, "Cannot open chip %s", chip);
            goto error;
        }

        struct gpiod_line_settings *settings = gpiod_line_settings_new();
        enum gpiod_line_direction direction =
            is_input ? GPIOD_LINE_DIRECTION_INPUT : GPIOD_LINE_DIRECTION_OUTPUT;

        gpiod_line_settings_set_direction(settings, direction);
        if (!is_input)
            gpiod_line_settings_set_output_value(settings, 0);

        struct gpiod_line_config *lcfg = gpiod_line_config_new();
        gpiod_line_config_add_line_settings(lcfg, &offset, 1, settings);

        struct gpiod_request_config *rcfg = gpiod_request_config_new();

        /* --- Dynamic consumer name based on IEC address --- */
        char consumer_name[64];
        snprintf(consumer_name, sizeof(consumer_name), "OpenPLC-%s", iec);
        gpiod_request_config_set_consumer(rcfg, consumer_name);

        struct gpiod_line_request *req = gpiod_chip_request_lines(gchip, rcfg, lcfg);
        if (!req)
        {
            plugin_logger_error(&g_logger, "Line request failed");
            gpiod_line_config_free(lcfg);
            gpiod_request_config_free(rcfg);
            gpiod_line_settings_free(settings);
            gpiod_chip_close(gchip);
            goto error;
        }

        if (is_input)
            inputs[byte][bit] = req;
        else
            outputs[byte][bit] = req;

        gpiod_line_config_free(lcfg);
        gpiod_request_config_free(rcfg);
        gpiod_line_settings_free(settings);
        gpiod_chip_close(gchip);
    }

    fclose(fp);
    io_initialized = 1;
    return 0;

error:
    fclose(fp);
    return -1;
}

/* ----------------- Plugin hooks ----------------- */
int init(void *args)
{
    plugin_logger_init(&g_logger, "GPIOD_PLUGIN", NULL);
    plugin_logger_info(&g_logger, "Initializing GPIOD plugin...");

    if (!args)
    {
        plugin_logger_error(&g_logger, "init args NULL");
        return -1;
    }
    // Copy the entire structure
    memcpy(&g_args, args, sizeof(plugin_runtime_args_t));
    // Now g_args can be safely used in start_loop, stop_loop, etc.

    plugin_logger_init(&g_logger, "GPIOD_PLUGIN", args);
    plugin_logger_info(&g_logger, "Plugin CSV: %s", g_args.plugin_specific_config_file_path);

    if (init_io_from_csv(g_args.plugin_specific_config_file_path) != 0)
    {
        plugin_logger_error(&g_logger, "Failed to initialize I/O from CSV");
        return -1;
    }

    plugin_initialized = 1;
    plugin_logger_info(&g_logger, "GPIOD plugin initialized successfully");
    return 0;
}

void start_loop(void)
{
    if (!plugin_initialized)
    {
        plugin_logger_error(&g_logger, "Cannot start - not initialized");
        return;
    }
    plugin_logger_info(&g_logger, "GPIOD plugin loop started");
    plugin_running = 1;
}

void stop_loop(void)
{
    if (!plugin_running)
        return;
    plugin_logger_info(&g_logger, "GPIOD plugin loop stopped");
    plugin_running = 0;
}

void cycle_start(void)
{
    enum gpiod_line_value val;

    if (!plugin_initialized || !plugin_running || !io_initialized)
        return;

    /* Buffer mutex already held by OpenPLC */
    for (int b = 0; b < BUFFER_SIZE; b++)
    {
        for (int bit = 0; bit < BITS_PER_BYTE; bit++)
        {
            if (inputs[b][bit])
            {
                val = gpiod_line_request_get_value(inputs[b][bit], 0);
                if (val != GPIOD_LINE_VALUE_ERROR)
                    *g_args.bool_input[b][bit] = val ? true : false;
            }
        }
    }
}

void cycle_end(void)
{
    enum gpiod_line_value val;

    if (!plugin_initialized || !plugin_running || !io_initialized)
        return;

    /* Buffer mutex already held by OpenPLC */
    for (int b = 0; b < BUFFER_SIZE; b++)
    {
        for (int bit = 0; bit < BITS_PER_BYTE; bit++)
        {
            if (outputs[b][bit])
            {
                val = *g_args.bool_output[b][bit] ? GPIOD_LINE_VALUE_ACTIVE
                                                  : GPIOD_LINE_VALUE_INACTIVE;
                gpiod_line_request_set_value(outputs[b][bit], 0, val);
            }
        }
    }
}

void cleanup(void)
{
    plugin_logger_info(&g_logger, "Cleaning up GPIOD plugin...");

    if (plugin_running)
        stop_loop();

    if (io_initialized)
    {
        for (int b = 0; b < BUFFER_SIZE; b++)
        {
            for (int bit = 0; bit < BITS_PER_BYTE; bit++)
            {
                if (inputs[b][bit])
                {
                    gpiod_line_request_release(inputs[b][bit]);
                    inputs[b][bit] = NULL;
                }
                if (outputs[b][bit])
                {
                    gpiod_line_request_release(outputs[b][bit]);
                    outputs[b][bit] = NULL;
                }
            }
        }
        io_initialized = 0;
    }

    plugin_initialized = 0;
    memset(&g_args, 0, sizeof(plugin_runtime_args_t));
    plugin_logger_info(&g_logger, "GPIOD plugin cleanup complete");
}
