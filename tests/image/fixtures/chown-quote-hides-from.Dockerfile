FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__ AS venvs
FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__ AS builder
FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__
COPY --chown="0 --from=venvs" --from=debian:trixie /etc/os-release /tmp/os-release
LABEL org.opencontainers.image.base.name="ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__" org.opencontainers.image.base.digest="__BASE_DIGEST__"
