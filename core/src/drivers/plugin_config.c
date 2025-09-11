#include "plugin_config.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int parse_plugin_config(const char *config_file, plugin_config_t *configs, int max_configs) {
    FILE *file = fopen(config_file, "r");
    if (!file) {
        return -1;
    }
    
    char line[512];
    int config_count = 0;
    
    while (fgets(line, sizeof(line), file) && config_count < max_configs) {
        // Skip comments and empty lines
        if (line[0] == '#' || line[0] == '\n' || line[0] == '\r') {
            continue;
        }
        
        // Parse plugin configuration: name,path,enabled,type,plugin_related_config_path
        // Parsing name
        char *token = strtok(line, ",");
        if (!token) continue; 
        strncpy(configs[config_count].name, token, sizeof(configs[config_count].name) - 1);
        
        // Parsing path
        token = strtok(NULL, ",");
        if (!token) continue;
        strncpy(configs[config_count].path, token, sizeof(configs[config_count].path) - 1);
        
        // Parsing enabled
        token = strtok(NULL, ",");
        if (!token) continue;
        configs[config_count].enabled = atoi(token);
        
        // Parsing type
        token = strtok(NULL, ",");
        if (!token) continue;
        configs[config_count].type = atoi(token);
        
        // parsing plugin_related_config_path
        token = strtok(NULL, ",");
        if (!token) continue;
        strncpy(configs[config_count].plugin_related_config_path, token, sizeof(configs[config_count].plugin_related_config_path) - 1);
        
        // Incrementing index to target next config
        config_count++;
    }
    
    fclose(file);
    return config_count;
}
