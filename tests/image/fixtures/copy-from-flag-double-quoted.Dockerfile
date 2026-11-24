FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b AS venvs
FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b AS builder
FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b
COPY --fr"om"=debian:trixie /etc/os-release /tmp/os-release
LABEL org.opencontainers.image.base.name="ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1" org.opencontainers.image.base.digest="sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b"
