#ifndef PLUGIN_MANAGER_H
#define PLUGIN_MANAGER_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C"
{
#endif

    typedef struct PluginManager PluginManager;

    /**
     * @brief Find the libplc_*.so file in the build directory
     *
     * @param[in]  build_dir  The build directory to search
     * @return A dynamically allocated string with the full path, or NULL on failure
     */
    char *find_libplc_file(const char *build_dir);

    /**
     * @brief Create a plugin manager for a given .so path
     *
     * @param[in]  so_path  The path to the .so file
     * @return A pointer to the created PluginManager, or NULL on failure
     */
    PluginManager *plugin_manager_create(const char *so_path);

    /**
     * @brief Destroy the plugin manager and unload the library
     *
     * @param[in]  pm  The plugin manager to destroy
     */
    void plugin_manager_destroy(PluginManager *pm);

    /**
     * @brief Ensure the library is loaded
     *
     * @param[in]  pm  The plugin manager to load
     * @return true if the library is loaded, false otherwise
     */
    bool plugin_manager_load(PluginManager *pm);

    /**
     * @brief Get a raw symbol (void*), you normally won’t call this directly
     *
     * @param[in]  pm  The plugin manager to get the symbol from
     * @param[in]  symbol_name  The name of the symbol to get
     * @return A pointer to the symbol, or NULL on failure
     */
    void *plugin_manager_get_symbol(PluginManager *pm, const char *symbol_name);

    /**
     * @brief Look up a symbol that is allowed to be missing, without logging.
     *
     * Same lookup as ::plugin_manager_get_symbol, but absence is not reported. Use
     * it for OPTIONAL parts of the .so contract, where a NULL result is a version
     * difference rather than a fault and the caller decides what (if anything) to
     * say about it -- see ::symbols_init, which resolves the retain marshalling and
     * the located-globals accessors this way.
     *
     * The distinction matters to the operator, not to the code: a program built by
     * an editor older than the feature is correct and runs correctly, so reporting
     * each absent symbol as an error described a healthy device as a broken one.
     *
     * Clears the pending dl error either way, so a later required lookup cannot
     * pick up this one's.
     *
     * @param[in]  pm  The plugin manager to get the symbol from
     * @param[in]  symbol_name  The name of the symbol to get
     * @return A pointer to the symbol, or NULL when it is not present
     */
    void *plugin_manager_try_get_symbol(PluginManager *pm, const char *symbol_name);
/**
 * @brief Type-safe function getter
 *
 * @param[in]  pm  The plugin manager to get the function from
 * @param[in]  type  The type of the function
 * @param[in]  name  The name of the function
 * @return A pointer to the function, or NULL on failure
 */
#define plugin_manager_get_func(pm, type, name) ((type)plugin_manager_get_symbol((pm), (name)))

#ifdef __cplusplus
}
#endif

#endif // PLUGIN_MANAGER_H
