// Command openplc-sidecar is the bootloader and update manager for one local
// OpenPLC runtime container (RTOP-283).
//
// It is always resident and, in steady state, does nothing: after confirming
// the runtime came up it blocks on the Docker events stream with no timers, no
// polling and no listening socket beyond its own control API. It exists so a
// device whose runtime will not start is still reachable from the editor, which
// is the whole point -- many vendors do not allow SSH.
//
// Docker is the only dependency. The sidecar itself is started by Docker's own
// restart policy, so nothing of ours goes into systemd.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/Autonomy-Logic/openplc-runtime/sidecar/internal/dockerapi"
	"github.com/Autonomy-Logic/openplc-runtime/sidecar/internal/health"
	"github.com/Autonomy-Logic/openplc-runtime/sidecar/internal/runtimespec"
	"github.com/Autonomy-Logic/openplc-runtime/sidecar/internal/supervisor"
)

// version is stamped at build time via -ldflags. The sidecar has its own
// version line, independent of the runtime's: it changes rarely, and coupling
// it to every runtime release would produce a long series of identical images.
var version = "dev"

// DefaultStateDir is the sidecar's own volume -- separate from the runtime's
// data directory on purpose. "Erase all data" wipes the runtime's volume, and
// the board's device mounts must survive that; a board that came back with no
// SPI after a data wipe would be a miserable failure mode.
const DefaultStateDir = "/var/lib/openplc-sidecar"

func main() {
	var (
		stateDir   = flag.String("state-dir", DefaultStateDir, "sidecar state directory")
		socket     = flag.String("docker-socket", dockerapi.DefaultSocket, "docker socket path")
		probeURL   = flag.String("probe-url", health.DefaultURL, "runtime health probe URL")
		showVer    = flag.Bool("version", false, "print version and exit")
		logLevel   = flag.String("log-level", "info", "log level: debug, info, warn, error")
		maxCrashes = flag.Int("max-crashes", supervisor.DefaultMaxCrashes,
			"unexpected runtime exits within the window before entering recovery")
		crashWindow = flag.Duration("crash-window", supervisor.DefaultCrashWindow,
			"sliding window for crash-loop detection")
	)
	flag.Parse()

	if *showVer {
		fmt.Println(version)
		return
	}

	log := newLogger(*logLevel)
	log.Info("openplc-sidecar starting", "version", version, "stateDir", *stateDir)

	if err := run(log, *stateDir, *socket, *probeURL, *maxCrashes, *crashWindow); err != nil {
		log.Error("sidecar exiting", "error", err)
		os.Exit(1)
	}
}

func run(
	log *slog.Logger,
	stateDir, socket, probeURL string,
	maxCrashes int,
	crashWindow time.Duration,
) error {
	if err := os.MkdirAll(stateDir, 0o750); err != nil {
		return fmt.Errorf("creating state dir %s: %w", stateDir, err)
	}

	specPath := filepath.Join(stateDir, "runtime-spec.json")
	spec, err := runtimespec.Load(specPath)
	if err != nil {
		// Without a spec the sidecar does not know which image to run or which
		// board mounts this device needs. Guessing would risk starting a
		// runtime with no access to its own hardware, so this is fatal and
		// install.sh is responsible for writing the file.
		return fmt.Errorf("%w (install.sh writes this file)", err)
	}
	log.Info("loaded runtime spec",
		"image", spec.ImageRef(), "dataDir", spec.DataDir, "extraBinds", len(spec.ExtraBinds))

	docker := dockerapi.New(socket)
	prober := health.New(probeURL, 5*time.Second)

	sup := supervisor.New(docker, spec, prober, supervisor.Config{
		MaxCrashes:  maxCrashes,
		CrashWindow: crashWindow,
	}, log.With("component", "supervisor"))

	// Signals: a container stop must not be read as a reason to tear the
	// runtime down. The sidecar going away leaves the runtime running, which
	// is correct -- losing the manager should never stop the plant.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := sup.Run(ctx); err != nil && ctx.Err() == nil {
		return err
	}
	log.Info("sidecar stopped; runtime container left running")
	return nil
}

func newLogger(level string) *slog.Logger {
	var lvl slog.Level
	switch level {
	case "debug":
		lvl = slog.LevelDebug
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		lvl = slog.LevelInfo
	}
	// Text, not JSON: the primary reader is a person running `docker logs`
	// against a device that is misbehaving.
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: lvl}))
}
