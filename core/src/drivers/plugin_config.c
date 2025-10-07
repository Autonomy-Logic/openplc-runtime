#include "plugin_config.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Helper function to remove newline characters from string
static void remove_newline(char *str)
{
    if (!str)
    {
        return;
    }

    // Remove \n, \r characters from the end of string
    char *end = str + strlen(str) - 1;
    while (end >= str && (*end == '\n' || *end == '\r' || *end == ' ' || *end == '\t'))
    {
        *end = '\0';
        end--;
    }
}

int parse_plugin_config(const char *config_file, plugin_config_t *configs, int max_configs)
{
    FILE *file = fopen(config_file, "r");
    if (!file)
    {
        return -1;
    }

    char line[512];
    int config_count = 0;

    while (fgets(line, sizeof(line), file) && config_count < max_configs)
    {
        // Skip comments and empty lines
        if (line[0] == '#' || line[0] == '\n' || line[0] == '\r')
        {
            continue;
        }

        // Parse plugin configuration: name,path,enabled,type,plugin_related_config_path
        // Parsing name
        char *token = strtok(line, ",");
        if (!token)
            continue;
        strncpy(configs[config_count].name, token, sizeof(configs[config_count].name) - 1);
        configs[config_count].name[sizeof(configs[config_count].name) - 1] = '\0';
        remove_newline(configs[config_count].name);

        // Parsing path
        token = strtok(NULL, ",");
        if (!token)
            continue;
        strncpy(configs[config_count].path, token, sizeof(configs[config_count].path) - 1);
        configs[config_count].path[sizeof(configs[config_count].path) - 1] = '\0';
        remove_newline(configs[config_count].path);

        // Parsing enabled
        token = strtok(NULL, ",");
        if (!token)
            continue;
        configs[config_count].enabled = atoi(token);

        // Parsing type
        token = strtok(NULL, ",");
        if (!token)
            continue;
        configs[config_count].type = atoi(token);

        // parsing plugin_related_config_path
        token = strtok(NULL, ",");
        if (!token)
            continue;
        strncpy(configs[config_count].plugin_related_config_path, token,
                sizeof(configs[config_count].plugin_related_config_path) - 1);
        configs[config_count]
            .plugin_related_config_path[sizeof(configs[config_count].plugin_related_config_path) -
                                        1] = '\0';
        remove_newline(configs[config_count].plugin_related_config_path);

        // parsing venv_path (optional field)
        token = strtok(NULL, ",\n\r");
        if (token)
        {
            strncpy(configs[config_count].venv_path, token,
                    sizeof(configs[config_count].venv_path) - 1);
            configs[config_count].venv_path[sizeof(configs[config_count].venv_path) - 1] = '\0';
            remove_newline(configs[config_count].venv_path);
        }
        else
        {
            // No venv_path specified, use empty string
            configs[config_count].venv_path[0] = '\0';
        }

        // Incrementing index to target next config
        config_count++;
    }

    fclose(file);
    return config_count;
}
