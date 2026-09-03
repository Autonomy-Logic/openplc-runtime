# syntax=docker/dockerfile:1

# Stub runtime for integration tests. See main.go for what it is and is not.
FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS build

ARG TARGETOS
ARG TARGETARCH

WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /out/stubruntime .

FROM scratch
COPY --from=build /out/stubruntime /stubruntime

EXPOSE 8443

# Same shape as the real runtime's healthcheck: an unauthenticated probe of
# /api/version. Exec form because there is no shell on scratch, and the probe
# lives in the binary so the image needs no curl either.
#
# Intervals are much tighter than the real image's. The real one allows 90s of
# start-period because a cold runtime loads plugin venvs; the stub is ready in
# milliseconds, and waiting 90s per case would make the suite unusable.
HEALTHCHECK --interval=2s --timeout=3s --start-period=2s --retries=2 \
    CMD ["/stubruntime", "-probe"]

ENTRYPOINT ["/stubruntime"]
