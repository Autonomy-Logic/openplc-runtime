# Benchmark-only image: the runtime under test with plc_main relinked to trace every scan body.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
COPY scan_trace.c /bench/scan_trace.c
RUN gcc -O2 -fPIC -Wall -Werror -c -I/workdir/core/src/plc_app /bench/scan_trace.c -o /bench/scan_trace.o \
    && cd /workdir/build \
    && cmake -DCMAKE_EXE_LINKER_FLAGS="-Wl,--wrap=scan_cycle_tracker_start -Wl,--wrap=scan_cycle_tracker_end /bench/scan_trace.o" .. >/dev/null \
    && make plc_main >/dev/null \
    && nm plc_main | grep -q __wrap_scan_cycle_tracker_end
