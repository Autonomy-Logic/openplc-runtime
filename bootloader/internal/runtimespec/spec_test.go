package runtimespec

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeSpec(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "runtime-spec.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("writing spec: %v", err)
	}
	return path
}

// --- loading -------------------------------------------------------------

func TestLoadAppliesDefaults(t *testing.T) {
	path := writeSpec(t, `{"version": "v4.2.1"}`)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if cfg.Repository != DefaultRepository {
		t.Fatalf("want default repository, got %q", cfg.Repository)
	}
	if cfg.DataDir != DefaultDataDir {
		t.Fatalf("want default data dir, got %q", cfg.DataDir)
	}
	if cfg.BootloaderPort != DefaultBootloaderPort {
		t.Fatalf("want default bootloader port, got %d", cfg.BootloaderPort)
	}
}

func TestLoadRejectsUnknownFields(t *testing.T) {
	// A typo in an operator-edited file must be reported, not ignored. A mount
	// that quietly did not apply is how a board comes up with no SPI and no
	// explanation.
	path := writeSpec(t, `{"version": "v4.2.1", "extraBind": ["/dev:/dev"]}`)
	if _, err := Load(path); err == nil {
		t.Fatal("a misspelled field must be rejected")
	}
}

func TestLoadRequiresAVersion(t *testing.T) {
	path := writeSpec(t, `{"repository": "example.com/x"}`)
	if _, err := Load(path); err == nil {
		t.Fatal("a spec with no version must be rejected")
	}
}

// --- bind validation -----------------------------------------------------

func TestTheDockerSocketCannotBeMountedIntoTheRuntime(t *testing.T) {
	// This is the whole security argument for the split: the bootloader holds the
	// socket, the runtime never does. Mounting it into the runtime would give
	// its HTTP API control of every container on the host.
	for _, bind := range []string{
		"/var/run/docker.sock:/var/run/docker.sock",
		"/run/docker.sock:/run/docker.sock:rw",
		// Path traversal must not get around the check.
		"/var/run/../run/docker.sock:/var/run/docker.sock",
	} {
		cfg := &Config{Version: "v1", ExtraBinds: []string{bind}}
		cfg.applyDefaults()
		err := cfg.Validate()
		if err == nil {
			t.Fatalf("bind %q must be refused", bind)
		}
		if !strings.Contains(err.Error(), "docker socket") {
			t.Fatalf("bind %q refused for the wrong reason: %v", bind, err)
		}
	}
}

func TestTheWholeHostFilesystemCannotBeMounted(t *testing.T) {
	cfg := &Config{Version: "v1", ExtraBinds: []string{"/:/host"}}
	cfg.applyDefaults()
	if err := cfg.Validate(); err == nil {
		t.Fatal("mounting / must be refused")
	}
}

func TestBindsMustBeWellFormedAndAbsolute(t *testing.T) {
	for _, bind := range []string{
		"/dev",                        // no target
		"dev:/dev",                    // relative source
		"/dev:dev",                    // relative target
		"/a:/b:ro:extra",              // too many parts
		"/lib/modules:/lib/modules:x", // bad mode
	} {
		cfg := &Config{Version: "v1", ExtraBinds: []string{bind}}
		cfg.applyDefaults()
		if err := cfg.Validate(); err == nil {
			t.Fatalf("malformed bind %q must be refused", bind)
		}
	}
}

func TestLegitimateBoardMountsAreAccepted(t *testing.T) {
	// The SLM-RP4 case: /dev covers SPI and GPIO, but a package that loads a
	// kernel module needs /lib/modules too.
	cfg := &Config{
		Version:    "v4.2.1",
		ExtraBinds: []string{"/lib/modules:/lib/modules:ro", "/etc/localtime:/etc/localtime:ro"},
		ExtraEnv:   []string{"TZ=America/New_York"},
	}
	cfg.applyDefaults()
	if err := cfg.Validate(); err != nil {
		t.Fatalf("a legitimate board mount must be accepted: %v", err)
	}
}

func TestExtraEnvMustBeKeyValue(t *testing.T) {
	cfg := &Config{Version: "v1", ExtraEnv: []string{"JUST_A_NAME"}}
	cfg.applyDefaults()
	if err := cfg.Validate(); err == nil {
		t.Fatal("an env entry without = must be refused")
	}
}

func TestVersionMustBeAUsableTag(t *testing.T) {
	for _, version := range []string{"v4.2 .1", "latest/stable", "with\ttab"} {
		cfg := &Config{Version: version}
		cfg.applyDefaults()
		if err := cfg.Validate(); err == nil {
			t.Fatalf("version %q must be refused", version)
		}
	}
}

// --- container spec ------------------------------------------------------

// decodeSpec renders ContainerSpec through JSON, which is what actually
// reaches the daemon -- asserting on the struct would miss a field that does
// not serialise.
func decodeSpec(t *testing.T, cfg *Config) map[string]any {
	t.Helper()
	encoded, err := json.Marshal(cfg.ContainerSpec(cfg.ImageRef()))
	if err != nil {
		t.Fatalf("marshalling spec: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(encoded, &out); err != nil {
		t.Fatalf("unmarshalling spec: %v", err)
	}
	return out
}

func TestContainerSpecCarriesTheParityFlags(t *testing.T) {
	cfg := &Config{Version: "v4.2.1"}
	cfg.applyDefaults()
	spec := decodeSpec(t, cfg)
	host := spec["HostConfig"].(map[string]any)

	if host["Privileged"] != true {
		t.Error("Privileged is required for /dev/mem, GPIO and SPI parity")
	}
	if host["NetworkMode"] != "host" {
		t.Errorf("NetworkMode must be host for EtherCAT and UDP discovery, got %v",
			host["NetworkMode"])
	}
	binds := host["Binds"].([]any)
	var sawDev bool
	for _, b := range binds {
		if b == "/dev:/dev" {
			sawDev = true
		}
	}
	if !sawDev {
		t.Error("/dev must be bound so hot-plugged devices appear without mknod")
	}
	if host["RestartPolicy"].(map[string]any)["Name"] != "no" {
		t.Error("the supervisor owns restarts; docker must not also restart it")
	}
}

func TestContainerSpecNeverSetsACPULimit(t *testing.T) {
	// The one trap that survives "just make it privileged", because it is not
	// a privilege: any of these enables the cgroup CPU controller, and with
	// CONFIG_RT_GROUP_SCHED a non-root cgroup starts at rt_runtime_us = 0, so
	// sched_setscheduler(SCHED_FIFO) fails and the runtime loses real-time
	// scheduling silently.
	cfg := &Config{Version: "v4.2.1"}
	cfg.applyDefaults()
	spec := decodeSpec(t, cfg)
	host := spec["HostConfig"].(map[string]any)

	for _, forbidden := range []string{
		"CpuQuota", "CpuPeriod", "NanoCpus", "Memory", "CpuShares", "CpuRealtimeRuntime",
	} {
		if _, present := host[forbidden]; present {
			t.Errorf("HostConfig must not carry %s: it would break SCHED_FIFO", forbidden)
		}
	}
}

func TestContainerSpecSetsTheRealTimeUlimits(t *testing.T) {
	cfg := &Config{Version: "v4.2.1"}
	cfg.applyDefaults()
	spec := decodeSpec(t, cfg)
	host := spec["HostConfig"].(map[string]any)

	limits := map[string]float64{}
	for _, raw := range host["Ulimits"].([]any) {
		entry := raw.(map[string]any)
		limits[entry["Name"].(string)] = entry["Soft"].(float64)
	}
	if limits["rtprio"] != 99 {
		t.Errorf("want rtprio 99, got %v", limits["rtprio"])
	}
	if limits["memlock"] != -1 {
		t.Errorf("want memlock unlimited, got %v", limits["memlock"])
	}
}

func TestContainerSpecTellsTheRuntimeItIsBootloaderManaged(t *testing.T) {
	// This is what makes /api/capabilities report updatePolicy "self". Only our
	// bootloader sets it, which is what makes the answer trustworthy -- an
	// orchestrator vPLC never gets it and so reports "managed".
	cfg := &Config{Version: "v4.2.1", BootloaderPort: 8445}
	cfg.applyDefaults()
	spec := decodeSpec(t, cfg)

	var sawPolicy, sawPort bool
	for _, raw := range spec["Env"].([]any) {
		switch raw.(string) {
		case "OPENPLC_UPDATE_POLICY=self":
			sawPolicy = true
		case "OPENPLC_BOOTLOADER_PORT=8445":
			sawPort = true
		}
	}
	if !sawPolicy {
		t.Error("the runtime must be told it is bootloader-managed")
	}
	if !sawPort {
		t.Error("the runtime must be told where the bootloader listens")
	}
}

func TestExtraBindsAreAppendedNotSubstituted(t *testing.T) {
	// An operator may add mounts; they must never be able to drop /dev or the
	// data directory by supplying their own list.
	cfg := &Config{Version: "v4.2.1", ExtraBinds: []string{"/lib/modules:/lib/modules:ro"}}
	cfg.applyDefaults()
	spec := decodeSpec(t, cfg)
	binds := spec["HostConfig"].(map[string]any)["Binds"].([]any)

	if len(binds) != 3 {
		t.Fatalf("want /dev, data dir and the extra bind, got %v", binds)
	}
}

// --- persistence ---------------------------------------------------------

func TestSaveThenLoadRoundTrips(t *testing.T) {
	// The bootloader rewrites version on a successful update; that choice has to
	// survive a reboot or the device would revert on next boot.
	path := writeSpec(t, `{"version": "v4.2.0"}`)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	cfg.Version = "v4.2.1"
	if err := cfg.Save(path); err != nil {
		t.Fatalf("save: %v", err)
	}
	reloaded, err := Load(path)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	if reloaded.Version != "v4.2.1" {
		t.Fatalf("want the saved version back, got %q", reloaded.Version)
	}
}

func TestSaveLeavesNoTempFileBehind(t *testing.T) {
	path := writeSpec(t, `{"version": "v4.2.0"}`)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if err := cfg.Save(path); err != nil {
		t.Fatalf("save: %v", err)
	}
	entries, err := os.ReadDir(filepath.Dir(path))
	if err != nil {
		t.Fatalf("reading dir: %v", err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".runtime-spec-") {
			t.Fatalf("temp file %q left behind", entry.Name())
		}
	}
}

func TestTheRuntimeIsPointedAtTheMountedDataDirectory(t *testing.T) {
	// The bind alone is not enough, and this is the bug that proved it on
	// hardware. The runtime resolves its persistent data directory by
	// DETECTION -- config.py returns /var/run/runtime whenever it thinks it
	// is containerized -- so without this override it writes a fresh .env and
	// restapi.db inside the container and ignores the mounted ones. Every
	// version swap would then discard users, credentials, the stored project,
	// retained variables and any VPP licenses.
	cfg := &Config{Version: "v4.2.1", DataDir: "/var/lib/openplc-runtime"}
	cfg.applyDefaults()
	spec := decodeSpec(t, cfg)

	var found bool
	for _, raw := range spec["Env"].([]any) {
		if raw.(string) == "OPENPLC_PERSISTENT_DATA_DIR=/var/lib/openplc-runtime" {
			found = true
		}
	}
	if !found {
		t.Fatalf("the runtime must be told to use the mounted data directory, got %v",
			spec["Env"])
	}

	// And the same path must actually be bound, or the override would point at
	// a directory that only exists inside the container.
	var bound bool
	for _, raw := range spec["HostConfig"].(map[string]any)["Binds"].([]any) {
		if raw.(string) == "/var/lib/openplc-runtime:/var/lib/openplc-runtime" {
			bound = true
		}
	}
	if !bound {
		t.Fatal("the data directory must be bind-mounted at the same path")
	}
}

func TestTheSocketDirectoryIsNotRedirected(t *testing.T) {
	// The command and log sockets are ephemeral and both endpoints live in the
	// same container, so they belong inside it. Redirecting them into the
	// shared volume would export container-internal plumbing onto the host
	// for no reason.
	cfg := &Config{Version: "v4.2.1"}
	cfg.applyDefaults()
	spec := decodeSpec(t, cfg)

	for _, raw := range spec["Env"].([]any) {
		if strings.HasPrefix(raw.(string), "OPENPLC_RUNTIME_DIR=") {
			t.Fatalf("the socket directory must keep its default, got %v", raw)
		}
	}
}
