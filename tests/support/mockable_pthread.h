#ifndef MOCKABLE_PTHREAD_H
#define MOCKABLE_PTHREAD_H

#include <pthread.h>

// Declare pthread functions that we want to mock
int pthread_mutex_init(pthread_mutex_t *mutex, const pthread_mutexattr_t *attr);
int pthread_mutex_destroy(pthread_mutex_t *mutex);
int pthread_mutex_lock(pthread_mutex_t *mutex);
int pthread_mutex_unlock(pthread_mutex_t *mutex);

#endif // MOCKABLE_PTHREAD_H