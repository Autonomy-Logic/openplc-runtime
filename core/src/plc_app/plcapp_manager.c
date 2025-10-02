#include "plcapp_manager.h"
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "utils/log.h"

struct PluginManager {
    char *so_path;
    void *handle;
};

PluginManager *plugin_manager_create(const char *so_path) 
{
    PluginManager *pm = calloc(1, sizeof(PluginManager));
    if (!pm)
    {
        return NULL;
    }
    pm->so_path = strdup(so_path);
    pm->handle = NULL;
    return pm;
}

void plugin_manager_destroy(PluginManager *pm) 
{
    if (!pm)
    {
        return;
    }
    if (pm->handle)
    {
        dlclose(pm->handle);
    }
    free(pm->so_path);
    free(pm);
}

bool plugin_manager_load(PluginManager *pm) 
{
    if (!pm)
    {
        return false;
    }
    if (pm->handle)
    {
        return true; // already loaded
    }

    pm->handle = dlopen(pm->so_path, RTLD_NOW);
    if (!pm->handle) 
    {
        log_error("Failed to load plugin %s: %s", pm->so_path, dlerror());
        return false;
    }
    return true;
}

void *plugin_manager_get_symbol(PluginManager *pm, const char *symbol_name) 
{
    if (!pm || !pm->handle)
    {
        return NULL;
    }
    dlerror(); // clear old error
    void *sym = dlsym(pm->handle, symbol_name);
    char *err = dlerror();
    if (err) 
    {
        log_error("dlsym error: %s", err);
        return NULL;
    }
    return sym;
}
