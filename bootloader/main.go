// Command openplc-bootloader brings up and maintains one local OpenPLC runtime
// container (RTOP-283).
//
// It plays the same role a bootloader plays on an embedded target, and the name
// is meant literally. A bootloader is the small, rarely-changed program that
// starts the real firmware, and that stays reachable to flash a new image when
// the firmware is broken or missing. This does exactly that for the runtime: it
// starts the runtime container, and when the runtime will not run it remains
// available so a new version can be installed from the editor. That is the
// whole reason it exists -- many vendors do not allow SSH, so without something
// that survives a bad runtime there is no way back onto the device.
//
// The analogy holds on the other axis too. A bootloader is kept deliberately
// dumb and stable because it is the one thing that cannot be recovered by any
// other means, so it does the minimum: it does not accept programs, control the
// PLC, or look at PLC state. It is always resident and, in steady state, does
// nothing at all -- after confirming the runtime came up it blocks on the
// Docker events stream, with no timers and no polling.
//
// Docker is the only dependency. Docker's own restart policy starts this
// process, so nothing of ours goes into systemd.
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

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/api"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/discovery"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/health"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimeauth"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/runtimespec"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/supervisor"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/updater"
)

// version is stamped at build time via -ldflags. The bootloader has its own
// version line, independent of the runtime's: it changes rarely, and coupling
// it to every runtime release would produce a long series of identical images.
var version = "dev"

// DefaultStateDir is the bootloader's own volume -- separate from the runtime's
// data directory on purpose. "Erase all data" wipes the runtime's volume, and
// the board's device mounts must survive that; a board that came back with no
// SPI after a data wipe would be a miserable failure mode.
const DefaultStateDir = "/var/lib/openplc-bootloader"

func main() {
	var (
		stateDir   = flag.String("state-dir", DefaultStateDir, "bootloader state directory")
		socket     = flag.String("docker-socket", dockerapi.DefaultSocket, "docker socket path")
		probeURL   = flag.String("probe-url", health.DefaultURL, "runtime health probe URL")
		showVer    = flag.Bool("version", false, "print version and exit")
		logLevel   = flag.String("log-level", "info", "log level: debug, info, warn, error")
		port       = flag.Int("port", api.DefaultPort, "control API port")
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
	log.Info("openplc-bootloader starting", "version", version, "stateDir", *stateDir)

	if err := run(log, runConfig{
		stateDir:    *stateDir,
		socket:      *socket,
		probeURL:    *probeURL,
		port:        *port,
		maxCrashes:  *maxCrashes,
		crashWindow: *crashWindow,
	}); err != nil {
		log.Error("bootloader exiting", "error", err)
		os.Exit(1)
	}
}

// runConfig groups what run needs, so adding a knob does not keep widening a
// positional parameter list.
type runConfig struct {
	stateDir    string
	socket      string
	probeURL    string
	port        int
	maxCrashes  int
	crashWindow time.Duration
}

func run(log *slog.Logger, cfg runConfig) error {
	if err := os.MkdirAll(cfg.stateDir, 0o750); err != nil {
		return fmt.Errorf("creating state dir %s: %w", cfg.stateDir, err)
	}

	specPath := filepath.Join(cfg.stateDir, "runtime-spec.json")
	spec, err := runtimespec.Load(specPath)
	if err != nil {
		// Without a spec the bootloader does not know which image to run or which
		// board mounts this device needs. Guessing would risk starting a
		// runtime with no access to its own hardware, so this is fatal and
		// install.sh is responsible for writing the file.
		return fmt.Errorf("%w (install.sh writes this file)", err)
	}
	log.Info("loaded runtime spec",
		"image", spec.ImageRef(), "dataDir", spec.DataDir, "extraBinds", len(spec.ExtraBinds))

	docker := dockerapi.New(cfg.socket)
	prober := health.New(cfg.probeURL, 5*time.Second)

	sup := supervisor.New(docker, spec, prober, supervisor.Config{
		MaxCrashes:  cfg.maxCrashes,
		CrashWindow: cfg.crashWindow,
	}, log.With("component", "supervisor"))

	// Authentication reads the runtime's own credentials out of the shared data
	// directory. Missing or unreadable is not fatal: the control API still
	// needs to come up so an operator can see WHY, and every authenticated
	// route refuses cleanly until the files appear.
	secrets, users := openRuntimeCredentials(log, spec.DataDir)
	if users != nil {
		defer users.Close()
	}

	// LAN discovery, answered ONLY while in recovery. A device that cannot be
	// found cannot be repaired, and without this a failed update makes the
	// device vanish from the editor's list at exactly the wrong moment. The
	// runtime owns this port the rest of the time; exclusivity holds because
	// entering recovery stops the runtime first.
	responder := discovery.New(discovery.Port, func() discovery.Reply {
		status := sup.Status()
		return discovery.Reply{
			BootloaderPort: spec.BootloaderPort,
			RuntimeVersion: spec.Version,
			Reason:         status.Reason,
		}
	}, log.With("component", "discovery"))

	sup.OnRecovery(func(supervisor.Status) { responder.Enable() })
	sup.OnHealthy(func(supervisor.Status) { responder.Disable() })

	upd := updater.New(updater.Config{
		Docker:     docker,
		Supervisor: sup,
		Spec:       spec,
		SpecPath:   specPath,
		StateDir:   cfg.stateDir,
		Log:        log.With("component", "updater"),
	})

	server, err := api.New(api.Config{
		Port:           cfg.port,
		StateDir:       cfg.stateDir,
		Version:        version,
		RuntimeVersion: func() string { return spec.Version },
		Secrets:        secrets,
		Users:          users,
		Supervisor:     sup,
		Logs:           docker,
		Updater:        upd,
		Log:            log.With("component", "api"),
	})
	if err != nil {
		return err
	}

	// Signals: a container stop must not be read as a reason to tear the
	// runtime down. The bootloader going away leaves the runtime running, which
	// is correct -- losing the manager should never stop the plant.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// The control API and the supervisor run concurrently, and the API must
	// outlive a supervisor that has given up: recovery mode is precisely the
	// state where the supervisor has stopped trying and an operator needs to
	// reach the device.
	apiErr := make(chan error, 1)
	go func() { apiErr <- server.ListenAndServe(ctx) }()

	supErr := make(chan error, 1)
	go func() { supErr <- sup.Run(ctx) }()

	select {
	case err := <-apiErr:
		// Losing the control API is fatal to the process: without it the
		// device is unmanageable, which is the one thing this binary exists
		// to prevent. Docker's restart policy brings us back.
		if err != nil && ctx.Err() == nil {
			return err
		}
	case err := <-supErr:
		if err != nil && ctx.Err() == nil {
			return err
		}
	case <-ctx.Done():
	}

	log.Info("bootloader stopped; runtime container left running")
	return nil
}

// openRuntimeCredentials loads the runtime's secrets and user database.
//
// Both live in the runtime's data directory, which the bootloader mounts
// read-only. Failure returns nils rather than an error on purpose: a device
// whose runtime has never started has neither file yet, and refusing to boot
// would leave nothing listening on the very device that most needs a way in.
func openRuntimeCredentials(
	log *slog.Logger, dataDir string,
) (*runtimeauth.Secrets, *runtimeauth.UserStore) {
	secrets, err := runtimeauth.LoadSecrets(filepath.Join(dataDir, ".env"))
	if err != nil {
		log.Warn("runtime secrets unavailable; authenticated routes will refuse",
			"error", err)
		return &runtimeauth.Secrets{}, nil
	}

	users, err := runtimeauth.OpenUserStore(filepath.Join(dataDir, "restapi.db"))
	if err != nil {
		log.Warn("runtime account database unavailable; authenticated routes will refuse",
			"error", err)
		return secrets, nil
	}
	return secrets, users
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
