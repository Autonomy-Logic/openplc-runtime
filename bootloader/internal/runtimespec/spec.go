// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Package runtimespec decides how the runtime container is run.
// Every flag below is load-bearing:
//
//   - Privileged + /dev bind: parity with the host-root install so
//     SPI_IOC_MESSAGE and GPIO line-handle ioctls reach real devices
//     and hot-plugged serial adapters appear without mknod.
//   - NetworkMode host: NICs under their real names for EtherCAT
//     AF_PACKET and UDP discovery broadcasts.
//   - UTSMode host: the device's live hostname, so discovery does
//     not report a container id captured at image build time.
//   - No CPU limits, ever: Cpus/CpuQuota/CpuPeriod/Memory enable the
//     cgroup CPU controller, and under CONFIG_RT_GROUP_SCHED a
//     non-root cgroup starts at rt_runtime_us=0, which makes
//     sched_setscheduler(SCHED_FIFO) fail. No fields exist for them.
//   - rtprio/memlock ulimits: redundant under Privileged (CAP_SYS_NICE
//     bypasses RLIMIT_RTPRIO, CAP_IPC_LOCK bypasses RLIMIT_MEMLOCK),
//     kept so a de-privileged container still works.
//   - RestartPolicy "no": the supervisor owns the lifecycle; a Docker
//     restart would race crash-loop accounting.
//
// Board-specific JSON additions may only ADD binds and environment;
// they cannot remove privilege, change network mode, or add CPU limits.
package runtimespec

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// Ulimit is Docker's rlimit shape.
type Ulimit struct {
	Name string `json:"Name"`
	Soft int64  `json:"Soft"`
	Hard int64  `json:"Hard"`
}

// RestartPolicy is Docker's restart-policy shape.
type RestartPolicy struct {
	Name string `json:"Name"`
}

// HostConfig is the subset of Docker's HostConfig we set. Fields we must never
// set are simply absent from the struct.
type HostConfig struct {
	Privileged    bool          `json:"Privileged"`
	NetworkMode   string        `json:"NetworkMode"`
	UTSMode       string        `json:"UTSMode"`
	Binds         []string      `json:"Binds"`
	Ulimits       []Ulimit      `json:"Ulimits"`
	RestartPolicy RestartPolicy `json:"RestartPolicy"`
}

// CreatePayload is the body of POST /containers/create.
type CreatePayload struct {
	Image      string     `json:"Image"`
	Env        []string   `json:"Env"`
	HostConfig HostConfig `json:"HostConfig"`
}

// Config is the operator-supplied part, read from disk.
type Config struct {
	// Repository is the image repository, without a tag.
	Repository string `json:"repository"`
	// Version is the tag currently desired. Writes come from the updater,
	// reads from the API, discovery and supervisor, so only touch through
	// Version()/SetVersion()+mu below -- direct access is a data race.
	Version string `json:"version"`
	// DataDir is the host path holding the runtime's persistent data. Bound
	// into the container at the same path so the runtime's own defaults apply
	// unchanged.
	DataDir string `json:"dataDir"`
	// ExtraBinds are additional host:container[:mode] mounts for boards that
	// need more than /dev -- /lib/modules for a package that loads a kernel
	// module, a vendor path, and so on.
	ExtraBinds []string `json:"extraBinds,omitempty"`
	// ExtraEnv are additional KEY=VALUE pairs.
	ExtraEnv []string `json:"extraEnv,omitempty"`
	// BootloaderPort is advertised to the runtime so /api/capabilities can tell
	// the editor where to send an update request.
	BootloaderPort int `json:"bootloaderPort,omitempty"`

	// mu guards Version. Not serialised: it is a lock, not configuration.
	mu sync.RWMutex `json:"-"`
}

const (
	DefaultRepository     = "ghcr.io/autonomy-logic/openplc-runtime"
	DefaultDataDir        = "/var/lib/openplc-runtime"
	DefaultBootloaderPort = 8445

	// UTSModeHost is Docker's "share the host's UTS namespace". Exported so
	// the supervisor compares against it rather than a second literal.
	UTSModeHost = "host"
)

// Host paths the runtime container must never mount. The docker socket
// is the one that matters: mounting it would hand the runtime's HTTP API
// control of every container on the host.
var forbiddenBindSources = []string{
	"/var/run/docker.sock",
	"/run/docker.sock",
}

// Load reads and validates a spec file, filling in defaults.
func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading runtime spec %s: %w", path, err)
	}
	var cfg Config
	// DisallowUnknownFields so a typo in an operator-edited file is reported
	// rather than silently ignored -- a mount that quietly did not apply is
	// how a board comes up with no SPI and no explanation.
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&cfg); err != nil {
		return nil, fmt.Errorf("parsing runtime spec %s: %w", path, err)
	}
	cfg.applyDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("invalid runtime spec %s: %w", path, err)
	}
	return &cfg, nil
}

// Save writes the config back, atomically, so a crash mid-write cannot leave
// the bootloader unable to parse its own spec on the next boot.
func (c *Config) Save(path string) error {
	// Under the read lock: the updater sets Version and saves, while the API
	// and the event loop read it. Marshalling without the lock would race the
	// very write this call exists to persist.
	c.mu.RLock()
	encoded, err := json.MarshalIndent(c, "", "  ")
	c.mu.RUnlock()
	if err != nil {
		return fmt.Errorf("encoding runtime spec: %w", err)
	}
	encoded = append(encoded, '\n')

	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".runtime-spec-*")
	if err != nil {
		return fmt.Errorf("creating temp spec in %s: %w", dir, err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op once the rename succeeds

	if _, err := tmp.Write(encoded); err != nil {
		tmp.Close()
		return fmt.Errorf("writing temp spec: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("syncing temp spec: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("closing temp spec: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("replacing spec %s: %w", path, err)
	}
	return nil
}

func (c *Config) applyDefaults() {
	if c.Repository == "" {
		c.Repository = DefaultRepository
	}
	if c.DataDir == "" {
		c.DataDir = DefaultDataDir
	}
	if c.BootloaderPort == 0 {
		c.BootloaderPort = DefaultBootloaderPort
	}
}

// Validate rejects a spec that would produce an unsafe or unusable container.
func (c *Config) Validate() error {
	if c.Version == "" {
		return errors.New("version is required")
	}
	if strings.ContainsAny(c.Version, " \t\n/") {
		return fmt.Errorf("version %q is not a valid image tag", c.Version)
	}
	if !filepath.IsAbs(c.DataDir) {
		return fmt.Errorf("dataDir %q must be an absolute path", c.DataDir)
	}
	if c.BootloaderPort < 1 || c.BootloaderPort > 65535 {
		return fmt.Errorf("bootloaderPort %d is out of range", c.BootloaderPort)
	}
	for _, bind := range c.ExtraBinds {
		if err := validateBind(bind); err != nil {
			return err
		}
	}
	for _, env := range c.ExtraEnv {
		if !strings.Contains(env, "=") {
			return fmt.Errorf("extraEnv entry %q is not KEY=VALUE", env)
		}
	}
	return nil
}

// validateBind enforces the shape and the safety rules for an operator-added
// mount.
func validateBind(bind string) error {
	parts := strings.Split(bind, ":")
	if len(parts) < 2 || len(parts) > 3 {
		return fmt.Errorf("bind %q must be host:container[:mode]", bind)
	}
	source, target := parts[0], parts[1]
	if !filepath.IsAbs(source) || !filepath.IsAbs(target) {
		return fmt.Errorf("bind %q must use absolute paths", bind)
	}
	if len(parts) == 3 && parts[2] != "ro" && parts[2] != "rw" {
		return fmt.Errorf("bind %q mode must be ro or rw", bind)
	}
	// Cleaning first so /a/../var/run/docker.sock does not slip past.
	cleaned := filepath.Clean(source)
	for _, forbidden := range forbiddenBindSources {
		if cleaned == forbidden {
			return fmt.Errorf(
				"bind %q is refused: mounting the docker socket into the runtime "+
					"would give its API control of the host", bind)
		}
	}
	if cleaned == "/" {
		return fmt.Errorf("bind %q is refused: the whole host filesystem", bind)
	}
	return nil
}

// ImageRef is the fully qualified image the runtime should run.
func (c *Config) ImageRef() string {
	return c.Repository + ":" + c.DesiredVersion()
}

// DesiredVersion returns the current desired tag under the mutex. The
// Version field stays exported for encoding/json; direct access races.
func (c *Config) DesiredVersion() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.Version
}

// SetDesiredVersion records a new desired tag.
func (c *Config) SetDesiredVersion(version string) {
	c.mu.Lock()
	c.Version = version
	c.mu.Unlock()
}

// ImageRefFor is ImageRef for an arbitrary version, used to pull a target
// before committing to it.
func (c *Config) ImageRefFor(version string) string {
	return c.Repository + ":" + version
}

// ContainerSpec builds the Docker create payload for imageRef.
func (c *Config) ContainerSpec(imageRef string) any {
	binds := []string{
		// Host devtmpfs: SPI, GPIO, I2C, serial. Live, so hot-plug works.
		"/dev:/dev",
		// Persistent data at the same path inside, so the runtime's own
		// defaults resolve without any env override.
		c.DataDir + ":" + c.DataDir,
	}
	binds = append(binds, c.ExtraBinds...)

	env := []string{
		// Override the runtime's auto-detected persistent dir so it writes
		// to the bind mount instead of /var/run/runtime inside the
		// container. RUNTIME_DIR keeps its default (sockets are ephemeral).
		"OPENPLC_PERSISTENT_DATA_DIR=" + c.DataDir,
	}
	env = append(env, c.ExtraEnv...)

	return CreatePayload{
		Image: imageRef,
		Env:   env,
		HostConfig: HostConfig{
			Privileged:  true,
			NetworkMode: "host",
			UTSMode:     UTSModeHost,
			Binds:       binds,
			Ulimits: []Ulimit{
				{Name: "rtprio", Soft: 99, Hard: 99},
				{Name: "memlock", Soft: -1, Hard: -1},
			},
			// The supervisor restarts it; Docker must not also try.
			RestartPolicy: RestartPolicy{Name: "no"},
		},
	}
}
