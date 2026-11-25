FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:2490fdf930f2743a6197b084ea3ebbe137d8d7cb0b8646e5c5dbbc62e8811b85 AS venvs

ARG SOURCE_DATE_EPOCH

WORKDIR /workdir

COPY requirements.txt pyproject.toml install.sh ./
COPY scripts/manage_plugin_venvs.sh scripts/
COPY core/src/drivers/plugins/python/modbus_master/requirements.txt core/src/drivers/plugins/python/modbus_master/
COPY core/src/drivers/plugins/python/modbus_slave/requirements.txt core/src/drivers/plugins/python/modbus_slave/
COPY core/src/drivers/plugins/python/opcua/requirements.txt core/src/drivers/plugins/python/opcua/

# The editable install records only the package path; the package arrives with the application layer.
RUN mkdir webserver && touch webserver/__init__.py && \
    ./install.sh --native deps venvs

FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:2490fdf930f2743a6197b084ea3ebbe137d8d7cb0b8646e5c5dbbc62e8811b85 AS builder

WORKDIR /workdir

COPY . .

RUN ./install.sh --native deps compile && \
    rm -rf build/CMakeFiles build/CMakeCache.txt build/Makefile build/cmake_install.cmake build/core

FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:2490fdf930f2743a6197b084ea3ebbe137d8d7cb0b8646e5c5dbbc62e8811b85

WORKDIR /workdir

COPY --from=venvs /workdir/venvs /workdir/venvs
COPY --from=builder /workdir/build /workdir/build
COPY --from=builder /workdir/.installed /workdir/.installed
COPY . .

LABEL org.opencontainers.image.base.name="ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1" \
      org.opencontainers.image.base.digest="sha256:2490fdf930f2743a6197b084ea3ebbe137d8d7cb0b8646e5c5dbbc62e8811b85"

ARG RUNTIME_VERSION=dev
ENV RUNTIME_VERSION=${RUNTIME_VERSION}

EXPOSE 8443

# /api/version answers without a token; /api/ping would always fail with 401.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -kfsS https://127.0.0.1:8443/api/version >/dev/null || exit 1

CMD ["bash", "./start_openplc.sh"]
