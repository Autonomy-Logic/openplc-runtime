#include "plugin_config.h"
#include "../plc_app/utils/log.h"
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

/**
 * Reject a plugin path that could point outside the runtime tree.
 *
 * The `path` field of a plugin config is handed straight to dlopen (VPP) or
 * used as a Python module location, so a config the runtime did not write is
 * arbitrary-code selection. vpp_plugins.conf IS such a config: it arrives
 * verbatim inside the user's upload. The Python side contains it too
 * (webserver/plcapp_management.py validate_vpp_plugins_conf); this check is
 * here so containment does not depend on one language alone.
 *
 * @param require_contained 0 to only reject `..` traversal, 1 to also reject
 *        absolute paths. Config files the runtime itself owns (plugins.conf)
 *        pass 0: an operator with a hand-written absolute path there is not
 *        the threat, and refusing it would break working installations. Only
 *        the upload-supplied config is parsed with 1.
 * @return 1 when the path is acceptable, 0 when it must be rejected.
 */
static int plugin_path_is_acceptable(const char *path, int require_contained)
{
    if (!path || path[0] == '\0')
    {
        return 0;
    }

    /* Any ".." component escapes, in every config, with no legitimate use. */
    const char *cursor = path;
    while (*cursor != '\0')
    {
        if (cursor[0] == '.' && cursor[1] == '.' &&
            (cursor[2] == '\0' || cursor[2] == '/' || cursor[2] == '\\'))
        {
            /* Only a ".." that starts a component counts, so a file legally
             * named "libfoo..so" is not rejected. */
            if (cursor == path || cursor[-1] == '/' || cursor[-1] == '\\')
            {
                return 0;
            }
        }
        cursor++;
    }

    if (require_contained)
    {
        if (path[0] == '/' || path[0] == '\\')
        {
            return 0;
        }
        /* Windows-style drive prefix ("C:\..."), reachable on the Cygwin build. */
        if (path[1] == ':')
        {
            return 0;
        }
    }

    return 1;
}

static int parse_plugin_config_internal(const char *config_file, plugin_config_t *configs,
                                        int max_configs, int require_contained)
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

        /* Containment: drop the whole entry rather than load from a path that
         * escapes the runtime tree. Skipping the entry (instead of aborting the
         * parse) keeps one bad line from disabling every other plugin. */
        if (!plugin_path_is_acceptable(configs[config_count].path, require_contained))
        {
            log_error("[PLUGIN] rejected plugin '%s' from %s: path '%s' is not contained in the "
                      "runtime tree",
                      configs[config_count].name, config_file, configs[config_count].path);
            continue;
        }

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

        // parsing plugin_related_config_path (optional field)
        token = strtok(NULL, ",");
        if (token && strlen(token) > 0)
        {
            log_debug("Found config_path: '%s'", token);
            strncpy(configs[config_count].plugin_related_config_path, token,
                    sizeof(configs[config_count].plugin_related_config_path) - 1);
            configs[config_count]
                .plugin_related_config_path[sizeof(configs[config_count].plugin_related_config_path) -
                                            1] = '\0';
            remove_newline(configs[config_count].plugin_related_config_path);
        }
        else
        {
            log_debug("No config_path found, using empty string");
            // No config path specified, use empty string
            configs[config_count].plugin_related_config_path[0] = '\0';
        }

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

int parse_plugin_config(const char *config_file, plugin_config_t *configs, int max_configs)
{
    return parse_plugin_config_internal(config_file, configs, max_configs, 0);
}

int parse_plugin_config_contained(const char *config_file, plugin_config_t *configs,
                                  int max_configs)
{
    return parse_plugin_config_internal(config_file, configs, max_configs, 1);
}
