/**
 * @file failinject.c
 * @brief LD_PRELOAD shim that fails exactly one pthread_create, on demand.
 *
 * Finding 4 lives on the "the transition worker could not be spawned" path, which
 * needs thread or memory exhaustion to reach -- not something to induce for real
 * inside a container without collateral damage.
 *
 * Contract: while the file named by FAILINJECT_ARM (default /tmp/failinject_arm)
 * exists, the NEXT pthread_create returns EAGAIN, and the shim then unlinks the
 * file itself. One-shot and self-disarming, so the failure lands on the call the
 * test aimed at and every later thread -- including the ones the recovery path
 * needs -- is created normally.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
#include <unistd.h>

typedef int (*pthread_create_fn)(pthread_t *, const pthread_attr_t *,
                                 void *(*)(void *), void *);

static const char *arm_path(void)
{
    const char *p = getenv("FAILINJECT_ARM");
    return (p && *p) ? p : "/tmp/failinject_arm";
}

int pthread_create(pthread_t *thread, const pthread_attr_t *attr,
                   void *(*start_routine)(void *), void *arg)
{
    static pthread_create_fn real = NULL;
    if (!real)
        real = (pthread_create_fn)dlsym(RTLD_NEXT, "pthread_create");

    const char *p = arm_path();
    if (access(p, F_OK) == 0)
    {
        /* Disarm first: if unlink fails we must not fail every subsequent call. */
        if (unlink(p) == 0)
            return EAGAIN;
    }

    return real(thread, attr, start_routine, arg);
}
