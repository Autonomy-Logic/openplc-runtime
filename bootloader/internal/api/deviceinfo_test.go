package api

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimeauth"
)

type fakeHost struct {
	info *dockerapi.Info
	err  error
}

func (f *fakeHost) SystemInfo(context.Context) (*dockerapi.Info, error) {
	return f.info, f.err
}

func newTestServerWithHost(t *testing.T, host HostReporter) *httptest.Server {
	t.Helper()
	srv := &Server{cfg: Config{
		Version:        "bootloader-v1.0.0-test",
		RuntimeVersion: func() string { return "v4.2.1" },
		Secrets:        &runtimeauth.Secrets{JWTSecret: testSecret, Pepper: testPepper},
		Users:          &fakeUsers{count: 1},
		Supervisor:     healthySupervisor(),
		Host:           host,
		Logs:           &fakeLogs{},
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	}}
	mux := http.NewServeMux()
	srv.routes(mux)
	httpSrv := httptest.NewServer(mux)
	t.Cleanup(httpSrv.Close)
	return httpSrv
}

// The reason this endpoint moved here from the runtime: the bootloader exists
// on every device that can be updated at all, so the answer does not depend on
// which runtime version happens to be installed.
func TestDeviceInfoReportsTheHostTheDaemonRunsOn(t *testing.T) {
	srv := newTestServerWithHost(t, &fakeHost{info: &dockerapi.Info{
		Name:            "slm-rp4",
		Architecture:    "aarch64",
		KernelVersion:   "6.12.35-rt10-v8+",
		OperatingSystem: "Debian GNU/Linux 12 (bookworm)",
		NCPU:            4,
		MemTotal:        1935417344,
		ServerVersion:   "20.10.24+dfsg1",
	}})

	resp, body := get(t, srv, "/api/bootloader/device-info", validToken(t))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("got HTTP %d", resp.StatusCode)
	}

	for field, want := range map[string]any{
		"hostname":     "slm-rp4",
		"architecture": "aarch64",
		"kernel":       "6.12.35-rt10-v8+",
		"system":       "Debian GNU/Linux 12 (bookworm)",
	} {
		if body[field] != want {
			t.Errorf("%s: got %v, want %v", field, body[field], want)
		}
	}

	if body["bootloaderVersion"] != "bootloader-v1.0.0-test" {
		t.Errorf("bootloaderVersion: got %v", body["bootloaderVersion"])
	}

	// Nothing here may be a constant. A field that always holds the same value
	// tells a reader only that this endpoint answered, which they already knew.
	for _, field := range []string{"containerized", "updatePolicy"} {
		if _, present := body[field]; present {
			t.Errorf("%s restates that a bootloader answered; it carries no information", field)
		}
	}
}

func TestDeviceInfoRequiresAToken(t *testing.T) {
	srv := newTestServerWithHost(t, &fakeHost{info: &dockerapi.Info{Name: "x"}})
	resp, _ := get(t, srv, "/api/bootloader/device-info", "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("got HTTP %d, want 401", resp.StatusCode)
	}
}

// A daemon that will not answer is not a fault worth failing the request over:
// the versions do not come from it, and a half-filled header beats an error.
func TestDeviceInfoSurvivesADaemonThatWillNotAnswer(t *testing.T) {
	srv := newTestServerWithHost(t, &fakeHost{err: errors.New("socket gone")})

	resp, body := get(t, srv, "/api/bootloader/device-info", validToken(t))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("got HTTP %d, want 200", resp.StatusCode)
	}
	if body["bootloaderVersion"] != "bootloader-v1.0.0-test" {
		t.Errorf("lost what does not come from the daemon: %v", body)
	}
	if _, present := body["hostname"]; present {
		t.Errorf("invented a hostname it could not read: %v", body["hostname"])
	}
}
