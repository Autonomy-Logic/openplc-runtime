// The sidecar keeps its dependencies to the minimum the job actually needs.
// It is the component that recovers a device when the runtime will not start,
// so every dependency is a way for that recovery to fail: the Docker Engine
// API is plain HTTP over a unix socket, JWT is an HMAC over two base64
// segments, and PBKDF2 is twenty lines of RFC 8018 -- all of which net/http
// and crypto/* already cover -- PBKDF2 is crypto/pbkdf2 as of Go 1.24.
//
// The one exception is modernc.org/sqlite. Authenticating a caller while the
// runtime is DOWN means reading the runtime's own users table, and cold
// recovery after a reboot is exactly when there is no runtime to ask. A
// second credential store in the sidecar would have avoided the dependency at
// the cost of another thing that can be forgotten when an account is revoked,
// which is the worse trade. Pure Go, so it still cross-compiles with CGO off
// and still runs on scratch.
module github.com/Autonomy-Logic/openplc-runtime/sidecar

go 1.25.0

require modernc.org/sqlite v1.58.0

require (
	github.com/dustin/go-humanize v1.0.1 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/mattn/go-isatty v0.0.24 // indirect
	github.com/ncruces/go-strftime v1.0.0 // indirect
	github.com/remyoudompheng/bigfft v0.0.0-20230129092748-24d4a6f8daec // indirect
	golang.org/x/sys v0.47.0 // indirect
	modernc.org/libc v1.75.6 // indirect
	modernc.org/mathutil v1.7.1 // indirect
	modernc.org/memory v1.12.1 // indirect
)
