#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <pthread.h>

// Define plugin_runtime_args_t structure locally (same as in plugin)
// TODO: Ideally, include from a shared header but avoiding Python dependencies here
typedef struct
{
    // Buffer pointers
    void *(*bool_input)[8];
    void *(*bool_output)[8];
    void **byte_input;
    void **byte_output;
    void **int_input;
    void **int_output;
    void **dint_input;
    void **dint_output;
    void **lint_input;
    void **lint_output;
    void **int_memory;
    void **dint_memory;
    void **lint_memory;

    // Mutex functions
    int (*mutex_take)(pthread_mutex_t *mutex);
    int (*mutex_give)(pthread_mutex_t *mutex);
    pthread_mutex_t *buffer_mutex;
    char plugin_specific_config_file_path[256];

    // Buffer size information
    int buffer_size;
    int bits_per_buffer;
} plugin_runtime_args_t;

// Function pointer types
typedef int (*plugin_init_func_t)(void *);

// Simple mutex functions for testing
int test_mutex_take(pthread_mutex_t *mutex) {
    return pthread_mutex_lock(mutex);
}

int test_mutex_give(pthread_mutex_t *mutex) {
    return pthread_mutex_unlock(mutex);
}

int main() {
    printf("Testing native plugin loading...\n");

    // Create a mock runtime args structure
    plugin_runtime_args_t args;
    memset(&args, 0, sizeof(plugin_runtime_args_t));

    // Initialize with test values
    args.buffer_size = 1024;
    args.bits_per_buffer = 8;
    strcpy(args.plugin_specific_config_file_path, "./test_config.ini");

    // Create a test mutex
    pthread_mutex_t test_mutex;
    pthread_mutex_init(&test_mutex, NULL);
    args.buffer_mutex = &test_mutex;
    args.mutex_take = test_mutex_take;
    args.mutex_give = test_mutex_give;

    // Load the plugin
    void *handle = dlopen("./test_plugin.so", RTLD_LAZY);
    if (!handle) {
        fprintf(stderr, "Failed to load plugin: %s\n", dlerror());
        return 1;
    }

    printf("Plugin loaded successfully!\n");

    // Clear any existing error
    dlerror();

    // Get the init function
    plugin_init_func_t init_func = (plugin_init_func_t)dlsym(handle, "init");
    if (!init_func) {
        fprintf(stderr, "Failed to find 'init' function: %s\n", dlerror());
        dlclose(handle);
        return 1;
    }

    printf("Found 'init' function!\n");

    // Call the init function
    int result = init_func(&args);
    if (result != 0) {
        fprintf(stderr, "Plugin init failed with code: %d\n", result);
        dlclose(handle);
        return 1;
    }

    printf("Plugin initialized successfully!\n");

    // Test other functions if they exist
    void (*start_func)() = (void (*)())dlsym(handle, "start_loop");
    if (start_func) {
        printf("Found 'start_loop' function, calling it...\n");
        start_func();
    } else {
        printf("'start_loop' function not found (optional)\n");
    }

    void (*stop_func)() = (void (*)())dlsym(handle, "stop_loop");
    if (stop_func) {
        printf("Found 'stop_loop' function, calling it...\n");
        stop_func();
    } else {
        printf("'stop_loop' function not found (optional)\n");
    }

    void (*cleanup_func)() = (void (*)())dlsym(handle, "cleanup");
    if (cleanup_func) {
        printf("Found 'cleanup' function, calling it...\n");
        cleanup_func();
    } else {
        printf("'cleanup' function not found (optional)\n");
    }

    // Close the plugin
    dlclose(handle);
    pthread_mutex_destroy(&test_mutex);

    printf("Plugin test completed successfully!\n");
    return 0;
}
