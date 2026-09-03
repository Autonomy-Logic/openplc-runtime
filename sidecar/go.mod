// The sidecar deliberately has no third-party dependencies. It is the
// component that recovers a device when the runtime will not start, so every
// dependency is a way for that recovery to fail. The Docker Engine API is
// plain HTTP over a unix socket, which net/http speaks natively.
module github.com/Autonomy-Logic/openplc-runtime/sidecar

go 1.23
