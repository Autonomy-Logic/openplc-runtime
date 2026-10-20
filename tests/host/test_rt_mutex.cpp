// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Host test: rt_mutex_init and RtMutex produce priority-inheritance mutexes.

#include "utils/rt_mutex.h"

#include <cstdio>
#include <mutex>
#include <thread>

static int g_failures = 0;

#define CHECK(cond, what)                                                                          \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  %s\n", what);                                                     \
            g_failures++;                                                                          \
        }                                                                                          \
    } while (0)

#if defined(__GLIBC__) && RT_MUTEX_HAS_PI
/* glibc internal kind bit for PTHREAD_PRIO_INHERIT mutexes. */
#define GLIBC_MUTEX_PRIO_INHERIT_NP 0x20
static bool is_pi(const pthread_mutex_t *m)
{
    return (m->__data.__kind & GLIBC_MUTEX_PRIO_INHERIT_NP) != 0;
}
#endif

int main()
{
    std::printf("rt_mutex: priority-inheritance initialisation\n");

    pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
    CHECK(rt_mutex_init(&m) == 0, "rt_mutex_init returns 0");
#if defined(__GLIBC__) && RT_MUTEX_HAS_PI
    CHECK(is_pi(&m), "rt_mutex_init sets PTHREAD_PRIO_INHERIT");
    pthread_mutex_t plain = PTHREAD_MUTEX_INITIALIZER;
    CHECK(!is_pi(&plain), "negative control: static initializer is not PI");
#endif
    CHECK(pthread_mutex_lock(&m) == 0, "PI mutex locks");
    CHECK(pthread_mutex_trylock(&m) != 0, "PI mutex is exclusive");
    CHECK(pthread_mutex_unlock(&m) == 0, "PI mutex unlocks");
    pthread_mutex_destroy(&m);

    RtMutex rm;
    int     counter = 0;
    auto    work    = [&]() {
        for (int i = 0; i < 100000; ++i)
        {
            std::lock_guard<RtMutex> g(rm);
            counter++;
        }
    };
    std::thread a(work), b(work);
    a.join();
    b.join();
    CHECK(counter == 200000, "RtMutex serialises std::lock_guard users");

    if (g_failures == 0)
        std::printf("all cases passed\n");
    return g_failures == 0 ? 0 : 1;
}
