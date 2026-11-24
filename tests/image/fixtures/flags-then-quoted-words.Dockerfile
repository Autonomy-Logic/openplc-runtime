FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b AS venvs
FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b AS builder
FROM ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1@sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b
COPY --chown=0:0 "requirements.txt" /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache echo --fr"om"=debian:trixie '--mo"unt"' \x
RUN ["sh", "-c", "echo --from=\"debian:trixie\""]
LABEL org.opencontainers.image.base.name="ghcr.io/autonomy-logic/openplc-runtime-base:trixie-1" org.opencontainers.image.base.digest="sha256:14009a363aed338e0dcf32b4f3e43bd2dff5a25ae75e65a4145726cde04cd62b"
