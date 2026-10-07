// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Command openplc-bootloader brings up and maintains one OpenPLC runtime
// container, and stays reachable to install a new version. Minimal:
// blocks on the Docker events stream, no PLC control or state inspection.
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
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/selfupdate"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/supervisor"
	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/updater"
)

// version is stamped at build time via -ldflags. The bootloader has its own
// version line, independent of the runtime's: it changes rarely, and coupling
// it to every runtime release would produce a long series of identical images.
var version = "dev"

// DefaultStateDir is the bootloader's own volume, deliberately separate
// from the runtime's data dir so "erase all data" leaves hardware mounts.
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

	// Self-update helper mode. A container cannot replace itself, so the
	// NEW image spawns this one-shot child to swap from outside. Must
	// never fall through into normal bootloader operation.
	if selfupdate.IsChild() {
		log.Info("running as a self-update helper", "version", version)
		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		if err := selfupdate.Execute(ctx, dockerapi.New(*socket), log.With("component", "selfupdate")); err != nil {
			log.Error("self-update failed", "error", err)
			os.Exit(1)
		}
		return
	}

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

	// Credentials re-read on use: the runtime may create .env and
	// restapi.db only after the bootloader is already up.
	creds := runtimeauth.NewProvider(spec.DataDir, log.With("component", "auth"))
	defer creds.Close()

	// LAN discovery, answered ONLY in recovery. Exclusive with the
	// runtime's own responder because recovery stops the runtime first.
	responder := discovery.New(discovery.Port, func() discovery.Reply {
		status := sup.Status()
		return discovery.Reply{
			BootloaderPort: spec.BootloaderPort,
			RuntimeVersion: spec.DesiredVersion(),
			Reason:         status.Reason,
		}
	}, log.With("component", "discovery"))

	sup.OnRecovery(func(supervisor.Status) { responder.Enable() })
	// Released before the runtime container starts, not when it reports
	// healthy: the runtime binds the discovery port once at start-up and never
	// retries, so a bootloader still holding it left the device
	// undiscoverable until the next restart.
	sup.OnRuntimeStarting(func() { responder.Disable() })
	// Belt and braces for a path that reaches healthy without going through
	// a start (adoption of an already-running container).
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
		RuntimeVersion: func() string { return spec.DesiredVersion() },
		Users:          creds,
		Supervisor:     sup,
		Host:           docker,
		Logs:           docker,
		Updater:        upd,
		SelfUpdater:    bootloaderSelfUpdater{docker: docker, log: log.With("component", "selfupdate")},
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

// bootloaderSelfUpdater adapts selfupdate to the API interface. Leaves
// the repository empty so the package default applies (never from an
// API caller's input: that would run arbitrary images as host root).
type bootloaderSelfUpdater struct {
	docker *dockerapi.Client
	log    *slog.Logger
}

func (b bootloaderSelfUpdater) Start(ctx context.Context, version string) error {
	// Repository is NEVER taken from the API request. The env override
	// exists for the integration harness and is set at install time.
	return selfupdate.Start(ctx, b.docker, os.Getenv("OPENPLC_BOOTLOADER_REPOSITORY"), version, b.log)
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
