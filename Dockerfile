# syntax=docker/dockerfile:1

FROM debian:bookworm-slim

# Runtime version baked in at build time (mirrors strucpp + editor —
# the value is the GitHub release tag, passed by .github/workflows/
# docker.yml).  Editors read this via /api/version to gate uploads
# (the v4.1.x runtime ships STruC++; older runtimes ship MatIEC).
ARG RUNTIME_VERSION=dev
ENV RUNTIME_VERSION=${RUNTIME_VERSION}

WORKDIR /workdir

# Copy source code
COPY . .

# Setup runtime directory and permissions
RUN mkdir -p /var/run/runtime && \
    chmod +x install.sh scripts/* start_openplc.sh

# Clean any existing build artifacts to ensure clean Docker build
RUN rm -rf build/ venvs/ .venv/ 2>/dev/null || true

# Run installation script
RUN ./install.sh

# Clean up apt cache to reduce image size (Docker-specific optimization)
RUN rm -rf /var/lib/apt/lists/*

# Expose webserver port
EXPOSE 8443

# Liveness for the bootloader (RTOP-283), which reads Docker's health state off
# the events stream instead of polling the runtime itself.
#
# /api/version, NOT /api/ping: ping sits behind @jwt_required(), so a
# healthcheck against it always gets a 401 and `curl -f` always fails. (The
# example in docs/DOCKER.md had exactly that bug.)
#
# Scope is deliberately "the webserver answers" and nothing more. Whether
# plc_main is running, whether a program is loaded, and whether that program
# is in ERROR are the webserver's own business -- runtimemanager._monitor()
# already restarts plc_main and drops it into safe mode on rapid crashes. If
# this probe cared about PLC state, a user uploading broken logic would make
# the container unhealthy and trigger a runtime recovery, turning a program
# bug into a device outage.
#
# start-period is generous because a cold start compiles nothing but does load
# plugin venvs, and a runtime marked unhealthy before it has finished booting
# would be restarted for no reason.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -kfsS https://127.0.0.1:8443/api/version >/dev/null || exit 1

# Default execution - Start OpenPLC Runtime
CMD ["bash", "./start_openplc.sh"]
