FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__ AS builder

RUN echo x<<EOF
FROM debian:trixie AS sneaky

FROM ghcr.io/autonomy-logic/openplc-runtime-base:__BASE_TAG__@__BASE_DIGEST__

COPY --from=sneaky /etc/os-release /etc/os-release \
EOF
