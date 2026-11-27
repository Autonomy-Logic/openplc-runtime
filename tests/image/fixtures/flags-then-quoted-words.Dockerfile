FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__ AS venvs
FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__ AS builder
FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__
COPY --chown=0:0 "requirements.txt" /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache echo --fr"om"=debian:trixie '--mo"unt"' \x
RUN ["sh", "-c", "echo --from=\"debian:trixie\""]
LABEL org.opencontainers.image.base.name="ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__" org.opencontainers.image.base.digest="__BASE_DIGEST__"
