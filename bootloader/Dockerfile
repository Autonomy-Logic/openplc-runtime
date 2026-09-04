# syntax=docker/dockerfile:1

# Bootloader image: a single static binary on scratch.
#
# Cross-compiled rather than emulated. Go targets every architecture we ship
# from one native builder, so buildx needs no QEMU here -- BUILDPLATFORM pins
# the build stage to the host and TARGETOS/TARGETARCH pick the output. That is
# the difference between seconds and the many minutes the runtime image spends
# under emulation.
#
# scratch, not alpine: this is the component that has to work when the runtime
# will not start, and every byte in the image is a byte that could stop it
# starting. There is no shell to debug with, which is the intended trade -- the
# bootloader's job is to report over HTTP, not to be poked at over exec.
FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS build

ARG TARGETOS
ARG TARGETARCH
# Bootloader version, independent of the runtime's. It changes rarely, so tying it
# to every runtime release would produce a long run of identical images.
ARG BOOTLOADER_VERSION=dev

WORKDIR /src

# Manifest and checksums first, so the dependency layer survives source-only
# changes. go.sum belongs here rather than arriving later with the source:
# without it `go mod download` fetches modernc.org/sqlite (the one third-party
# dependency, for reading the runtime's user database) unverified, and the
# pinning only happens by accident when COPY . . brings the real go.sum in.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# CGO off so the result is genuinely static and runs on scratch.
# -trimpath and -s -w keep the binary small and reproducible.
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build \
    -trimpath \
    -ldflags="-s -w -X main.version=${BOOTLOADER_VERSION}" \
    -o /out/openplc-bootloader .

# Fail the build rather than ship an untested bootloader. Tests run on the build
# platform, which is where they are meaningful -- the logic under test is a
# state machine, not anything architecture-specific.
RUN CGO_ENABLED=0 go test ./...

FROM scratch

ARG BOOTLOADER_VERSION=dev
LABEL org.opencontainers.image.title="OpenPLC Runtime Bootloader" \
      org.opencontainers.image.description="Bootloader and update manager for a local OpenPLC runtime" \
      org.opencontainers.image.source="https://github.com/Autonomy-Logic/openplc-runtime" \
      org.opencontainers.image.version="${BOOTLOADER_VERSION}"

# CA roots for any future outbound HTTPS. Nothing needs them today -- image
# pulls go through the Docker daemon, which has its own -- but a missing root
# store fails in a way that is genuinely hard to diagnose from a scratch image.
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

COPY --from=build /out/openplc-bootloader /openplc-bootloader

# Control API. 8445 keeps to the odd numbers alongside the runtime's 8443.
EXPOSE 8445

ENTRYPOINT ["/openplc-bootloader"]
