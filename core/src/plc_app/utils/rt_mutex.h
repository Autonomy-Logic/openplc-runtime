// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef RT_MUTEX_H
#define RT_MUTEX_H

#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

/* PTHREAD_PRIO_INHERIT is optional in POSIX; MSYS2/Cygwin lack it. */
#if !defined(__CYGWIN__) && !defined(__MSYS__) && defined(_POSIX_THREAD_PRIO_INHERIT) &&           \
    _POSIX_THREAD_PRIO_INHERIT > 0
#define RT_MUTEX_HAS_PI 1
#else
#define RT_MUTEX_HAS_PI 0
#endif

/**
 * @brief Initialise a mutex with the priority-inheritance protocol.
 *
 * A mutex shared between a SCHED_FIFO thread and a SCHED_OTHER thread must use
 * priority inheritance, or a preempted low-priority holder can block the
 * real-time thread forever on a single CPU. On platforms without PI the mutex
 * is initialised with default attributes.
 *
 * The mutex is left untouched when this fails, so a statically initialised
 * mutex (PTHREAD_MUTEX_INITIALIZER) stays usable as a plain mutex.
 *
 * @param m mutex to initialise; must not be locked or in use
 * @return 0 on success, an errno value otherwise
 */
static inline int rt_mutex_init(pthread_mutex_t *m)
{
    pthread_mutexattr_t attr;
    int rc = pthread_mutexattr_init(&attr);
    if (rc != 0)
        return rc;
#if RT_MUTEX_HAS_PI
    rc = pthread_mutexattr_setprotocol(&attr, PTHREAD_PRIO_INHERIT);
    if (rc != 0)
    {
        pthread_mutexattr_destroy(&attr);
        return rc;
    }
#endif
    rc = pthread_mutex_init(m, &attr);
    pthread_mutexattr_destroy(&attr);
    return rc;
}

/**
 * @brief Re-initialise a statically initialised mutex with priority inheritance.
 *
 * For use from constructors that run before the logger exists: a failure is
 * reported on stderr and the mutex keeps its plain static initialisation.
 *
 * @param m    mutex initialised with PTHREAD_MUTEX_INITIALIZER, not yet in use
 * @param name label for the stderr report
 */
static inline void rt_mutex_upgrade_static(pthread_mutex_t *m, const char *name)
{
    int rc = rt_mutex_init(m);
    if (rc != 0)
        fprintf(stderr, "[rt_mutex] %s: PI init failed (%s), using a plain mutex\n", name,
                strerror(rc));
}

#ifdef __cplusplus
#include <cstdio>
#include <cstdlib>
#include <cstring>

/**
 * @brief Priority-inheritance replacement for std::mutex.
 *
 * Satisfies BasicLockable, so it works with std::lock_guard and
 * std::unique_lock. std::mutex cannot take mutex attributes.
 */
class RtMutex
{
  public:
    RtMutex()
    {
        int rc = rt_mutex_init(&m_);
        if (rc != 0)
        {
            std::fprintf(stderr, "[rt_mutex] PI init failed (%s), using a plain mutex\n",
                         std::strerror(rc));
            if (pthread_mutex_init(&m_, nullptr) != 0)
                std::abort();
        }
    }
    ~RtMutex()
    {
        pthread_mutex_destroy(&m_);
    }
    RtMutex(const RtMutex &)            = delete;
    RtMutex &operator=(const RtMutex &) = delete;

    void lock()
    {
        pthread_mutex_lock(&m_);
    }
    void unlock()
    {
        pthread_mutex_unlock(&m_);
    }

  private:
    pthread_mutex_t m_;
};
#endif

#endif // RT_MUTEX_H
