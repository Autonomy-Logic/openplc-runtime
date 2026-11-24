FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b AS venvs
ONBUILD COPY --from=debian:trixie /etc/os-release /tmp/os-release
FROM venvs AS builder
FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b
COPY --from=builder /tmp/os-release /etc/os-release
LABEL org.opencontainers.image.base.name="ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1" org.opencontainers.image.base.digest="sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b"
