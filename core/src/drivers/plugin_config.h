#ifndef PLUGIN_CONFIG_H
#define PLUGIN_CONFIG_H

#define MAX_PLUGIN_NAME_LEN 64
#define MAX_PLUGIN_PATH_LEN 256

typedef struct
{
    char name[MAX_PLUGIN_NAME_LEN];
    char path[MAX_PLUGIN_PATH_LEN];
    int enabled;
    int type; // 0 = python, 1 = native
    char plugin_related_config_path[MAX_PLUGIN_PATH_LEN];
    char venv_path[MAX_PLUGIN_PATH_LEN]; // Path to virtual environment
} plugin_config_t;

/**
 * Parse a plugin config the runtime owns (plugins.conf).
 *
 * Entries whose `path` contains a ".." component are rejected and skipped;
 * absolute paths are allowed, because an operator may legitimately point a
 * hand-written plugins.conf at one.
 */
int parse_plugin_config(const char *config_file, plugin_config_t *configs, int max_configs);

/**
 * Parse a plugin config that came from an upload (vpp_plugins.conf).
 *
 * As above, plus absolute (and Windows drive-prefixed) paths are rejected: the
 * `path` field of this file is chosen by whoever produced the upload and is fed
 * to dlopen, so it must stay inside the runtime tree. Mirrors the Python-side
 * containment in webserver/plcapp_management.py so neither side is the only
 * thing standing between an upload and dlopen.
 */
int parse_plugin_config_contained(const char *config_file, plugin_config_t *configs,
                                  int max_configs);

#endif // PLUGIN_CONFIG_H
