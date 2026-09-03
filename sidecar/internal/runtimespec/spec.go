// Package runtimespec decides how the runtime container is run.
//
// This is the ONE place those flags exist. The plan settled on a single
// privilege level rather than a matrix of profiles, because multiple profiles
// mean multiple ways to be misconfigured and a support matrix nobody can hold
// in their head. Every flag below is load-bearing:
//
//   - Privileged + /dev bind: exact parity with the current root install.
//     Verified against the SLM-RP4 HAL, which drives /dev/spidev6.0 through
//     SPI_IOC_MESSAGE and /dev/gpiochip0 through the GPIO line-handle ioctls.
//     Binding the host's live devtmpfs also means hot-plugged serial adapters
//     appear without mknod or device cgroup rules.
//
//   - NetworkMode host: every NIC visible under its real name in the host's
//     own namespace. EtherCAT needs AF_PACKET and SIOCSIFFLAGS on a real
//     interface, and the UDP discovery responder needs to see broadcasts.
//     Deliberately NOT the orchestrator's dedicated-NIC mechanism, which moves
//     a host NIC into a container namespace and removes it from the host.
//
//   - No CPU limits, ever. This is the one trap that survives "just make it
//     privileged", because it is not a privilege. Setting Cpus/CpuQuota/
//     CpuPeriod/Memory enables the cgroup CPU controller, and with
//     CONFIG_RT_GROUP_SCHED a non-root cgroup starts at rt_runtime_us = 0 --
//     at which point sched_setscheduler(SCHED_FIFO) fails outright and the
//     runtime silently loses real-time scheduling. There is no field for them
//     in this package, so they cannot be set by accident. CpusetCpus would be
//     safe (pinning is not bandwidth throttling) but nothing needs it yet.
//
//   - rtprio/memlock ulimits are redundant under Privileged, since
//     CAP_SYS_NICE bypasses RLIMIT_RTPRIO and CAP_IPC_LOCK bypasses
//     RLIMIT_MEMLOCK. They stay as documented intent, and they are what saves
//     the deployment if anyone ever de-privileges the container.
//
//   - RestartPolicy "no": the supervisor owns the lifecycle. Letting Docker
//     also restart it would race the crash-loop accounting and hide exactly
//     the signal recovery mode depends on.
//
// Board-specific additions come from a JSON file in the sidecar's own volume,
// written by install.sh. That file may only ADD binds and environment; it can
// never remove privilege, change the network mode, or introduce a CPU limit.
// Validation is strict because the file is the one operator-supplied input to
// a component that runs as host root.
package runtimespec

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
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
	// Version is the tag currently desired. The sidecar rewrites this when an
	// update succeeds, which is what makes the choice survive a reboot.
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
	// SidecarPort is advertised to the runtime so /api/capabilities can tell
	// the editor where to send an update request.
	SidecarPort int `json:"sidecarPort,omitempty"`
}

const (
	DefaultRepository  = "ghcr.io/autonomy-logic/openplc-runtime"
	DefaultDataDir     = "/var/lib/openplc-runtime"
	DefaultSidecarPort = 8445
)

// forbiddenBindTargets are host paths that must never be handed to the runtime
// container. The docker socket is the important one: mounting it would give
// the runtime's HTTP API control of every container on the host, which is
// precisely the privilege the sidecar exists to keep away from it.
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
// the sidecar unable to parse its own spec on the next boot.
func (c *Config) Save(path string) error {
	encoded, err := json.MarshalIndent(c, "", "  ")
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
	if c.SidecarPort == 0 {
		c.SidecarPort = DefaultSidecarPort
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
	if c.SidecarPort < 1 || c.SidecarPort > 65535 {
		return fmt.Errorf("sidecarPort %d is out of range", c.SidecarPort)
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
	return c.Repository + ":" + c.Version
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
		// Tells /api/capabilities to report updatePolicy "self". Only our
		// sidecar sets this, which is what makes the answer trustworthy.
		"OPENPLC_UPDATE_POLICY=self",
		fmt.Sprintf("OPENPLC_SIDECAR_PORT=%d", c.SidecarPort),
	}
	env = append(env, c.ExtraEnv...)

	return CreatePayload{
		Image: imageRef,
		Env:   env,
		HostConfig: HostConfig{
			Privileged:  true,
			NetworkMode: "host",
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
