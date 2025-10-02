#ifndef MOCKABLE_STDLIB_H
#define MOCKABLE_STDLIB_H

#include <stdlib.h>

// Declare functions that we want to mock
void* calloc(size_t num, size_t size);
void free(void* ptr);
void* malloc(size_t size);

#endif // MOCKABLE_STDLIB_H