FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__ AS venvs

WORKDIR /workdir

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3-venv python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml install.sh ./
COPY scripts/manage_plugin_venvs.sh scripts/
COPY core/src/drivers/plugins/python/modbus_master/requirements.txt core/src/drivers/plugins/python/modbus_master/
COPY core/src/drivers/plugins/python/modbus_slave/requirements.txt core/src/drivers/plugins/python/modbus_slave/
COPY core/src/drivers/plugins/python/opcua/requirements.txt core/src/drivers/plugins/python/opcua/

RUN ./install.sh --native

FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__ AS builder

WORKDIR /workdir

RUN apt-get update && \
    apt-get install -y --no-install-recommends cmake python3-dev pkg-config git && \
    rm -rf /var/lib/apt/lists/*

COPY . .

RUN ./install.sh --native

FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__

WORKDIR /workdir

COPY --from=venvs /workdir/venvs /workdir/venvs
COPY --from=builder /workdir/build /workdir/build
COPY --from=builder /workdir/.installed /workdir/.installed
COPY webserver webserver
COPY . .

LABEL org.opencontainers.image.base.name="ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__" \
      org.opencontainers.image.base.digest="__BASE_DIGEST__"

LABEL org.opencontainers.image.base.digest="sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

ARG RUNTIME_VERSION=dev
ENV RUNTIME_VERSION=${RUNTIME_VERSION}

EXPOSE 8443

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -kfsS https://127.0.0.1:8443/api/version >/dev/null || exit 1

CMD ["bash", "./start_openplc.sh"]
