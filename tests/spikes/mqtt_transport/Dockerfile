# Spike runner for the MQTT transport/codec evidence. Not a product image.
FROM python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

RUN apt-get update \
    && apt-get install -y --no-install-recommends protobuf-compiler curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
RUN set -eu; \
    case "$TARGETARCH" in \
      arm64) asset=linux-aarch_64; sum=8b8f18bd2b30346efbc698dd5a73dd7c805f3ef8380f6dfc95c768f3f1852f6a ;; \
      amd64) asset=linux-x86_64; sum=121f6c7afe1d4d0e3ea6aab9432038599250134cbf4474cb1167d2c7decd4278 ;; \
      *) echo "unsupported arch $TARGETARCH"; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/protoc.zip "https://github.com/protocolbuffers/protobuf/releases/download/v36.2/protoc-36.2-${asset}.zip"; \
    echo "${sum}  /tmp/protoc.zip" | sha256sum -c -; \
    unzip -q -d /opt/protoc36 /tmp/protoc.zip; rm /tmp/protoc.zip

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.lock

COPY sparkplug_b.proto /opt/sparkplug/sparkplug_b.proto
RUN echo "4432c5c483b7fb9732d0594c98a2e97dca5e517e39c5374a8b918d837f0b4a19  /opt/sparkplug/sparkplug_b.proto" | sha256sum -c - \
    && mkdir -p /opt/gen \
    && /opt/protoc36/bin/protoc --proto_path=/opt/sparkplug --python_out=/opt/gen /opt/sparkplug/sparkplug_b.proto

ENV PYTHONPATH=/opt/gen PYTHONDONTWRITEBYTECODE=1
WORKDIR /spike
