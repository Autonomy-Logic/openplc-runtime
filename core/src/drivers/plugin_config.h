#ifndef PLUGIN_CONFIG_H
#define PLUGIN_CONFIG_H

#define MAX_PLUGIN_NAME_LEN 64
#define MAX_PLUGIN_PATH_LEN 256

typedef struct
{
    char name[MAX_PLUGIN_NAME_LEN];
    char path[MAX_PLUGIN_PATH_LEN];
    int enabled;
    int type; // 0 = native, 1 = python
    char plugin_related_config_path[MAX_PLUGIN_PATH_LEN];
    char venv_path[MAX_PLUGIN_PATH_LEN]; // Path to virtual environment
} plugin_config_t;

int parse_plugin_config(const char *config_file, plugin_config_t *configs, int max_configs);

#endif // PLUGIN_CONFIG_H
