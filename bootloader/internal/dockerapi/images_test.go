package dockerapi

import (
	"sync"
	"testing"
	"time"
)

func TestSplitImageRefUsesTheLastColonAfterTheLastSlash(t *testing.T) {
	// A registry with a port puts a colon in the NAME. Splitting on the first
	// colon would turn "host:5000/openplc:v1" into name "host" and tag
	// "5000/openplc:v1", and the pull would go somewhere that does not exist.
	cases := []struct {
		ref       string
		wantName  string
		wantTag   string
		rationale string
	}{
		{
			"ghcr.io/autonomy-logic/openplc-runtime:v4.2.1",
			"ghcr.io/autonomy-logic/openplc-runtime", "v4.2.1",
			"the ordinary case",
		},
		{
			"registry.local:5000/openplc-runtime:v4.2.1",
			"registry.local:5000/openplc-runtime", "v4.2.1",
			"a registry port must not be mistaken for the tag",
		},
		{
			"registry.local:5000/openplc-runtime",
			"registry.local:5000/openplc-runtime", "latest",
			"an untagged reference on a ported registry defaults to latest",
		},
		{
			"openplc-runtime",
			"openplc-runtime", "latest",
			"a bare name defaults to latest, as docker itself does",
		},
	}
	for _, c := range cases {
		name, tag := splitImageRef(c.ref)
		if name != c.wantName || tag != c.wantTag {
			t.Errorf("%s: splitImageRef(%q) = (%q, %q), want (%q, %q)",
				c.rationale, c.ref, name, tag, c.wantName, c.wantTag)
		}
	}
}

func TestProgressAggregatesAcrossLayers(t *testing.T) {
	// Reporting whichever layer spoke last would make the bar jump around;
	// the figure a user watches has to be over the whole image.
	layers := map[string]struct{ current, total int64 }{
		"a": {current: 50, total: 100},
		"b": {current: 25, total: 100},
	}
	progress := aggregate("Downloading", layers)
	if progress.Percent == nil {
		t.Fatal("want a percentage when totals are known")
	}
	if *progress.Percent != 37 {
		t.Fatalf("want 37%% across both layers, got %d", *progress.Percent)
	}
	if progress.Current != 75 || progress.Total != 200 {
		t.Fatalf("want 75/200, got %d/%d", progress.Current, progress.Total)
	}
}

func TestProgressHasNoPercentageWithoutTotals(t *testing.T) {
	// The daemon gives no size for layers it already has, and for the window
	// before any size is known. Reporting 0% there would look like a stall.
	progress := aggregate("Pulling fs layer", map[string]struct{ current, total int64 }{})
	if progress.Percent != nil {
		t.Fatalf("want no percentage, got %d", *progress.Percent)
	}
	if progress.Phase != "Pulling fs layer" {
		t.Fatalf("the daemon's own wording must pass through, got %q", progress.Phase)
	}
}

func TestProgressIsClampedTo100(t *testing.T) {
	// The daemon occasionally reports a layer's current slightly above its
	// total, and a progress bar reading 103% looks like a bug.
	layers := map[string]struct{ current, total int64 }{
		"a": {current: 110, total: 100},
	}
	progress := aggregate("Extracting", layers)
	if *progress.Percent != 100 {
		t.Fatalf("want the percentage clamped to 100, got %d", *progress.Percent)
	}
}

// --- stall watchdog ------------------------------------------------------

func TestTheStallWatchdogFiresWhenProgressStops(t *testing.T) {
	// Docker's streaming pull takes no timeout, so a half-open connection to
	// the registry parks the decoder forever. This is what turns that into a
	// reported failure instead of a permanently "pulling" device.
	var (
		mu    sync.Mutex
		fired bool
	)
	w := newStallWatchdog(20*time.Millisecond, func() {
		mu.Lock()
		fired = true
		mu.Unlock()
	})
	defer w.stop()

	time.Sleep(80 * time.Millisecond)
	mu.Lock()
	got := fired
	mu.Unlock()

	if !got {
		t.Fatal("the watchdog must fire after the stall timeout")
	}
	if !w.fired() {
		t.Fatal("fired() must report the trip, so the error can say 'no progress' " +
			"rather than blaming the transport")
	}
}

func TestABeatKeepsTheWatchdogQuiet(t *testing.T) {
	// A slow but progressing pull must not be aborted: on a plant link a
	// large image can legitimately take a very long time.
	var (
		mu    sync.Mutex
		fired bool
	)
	w := newStallWatchdog(60*time.Millisecond, func() {
		mu.Lock()
		fired = true
		mu.Unlock()
	})
	defer w.stop()

	for i := 0; i < 6; i++ {
		time.Sleep(20 * time.Millisecond)
		w.beat()
	}

	mu.Lock()
	got := fired
	mu.Unlock()
	if got {
		t.Fatal("a pull that keeps reporting progress must not be abandoned")
	}
}

func TestStoppingTheWatchdogPreventsALateFire(t *testing.T) {
	// Without this, a completed pull could still have its context cancelled a
	// moment later, and the next operation would fail for no reason.
	var (
		mu    sync.Mutex
		fired bool
	)
	w := newStallWatchdog(20*time.Millisecond, func() {
		mu.Lock()
		fired = true
		mu.Unlock()
	})
	w.stop()

	time.Sleep(60 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if fired {
		t.Fatal("a stopped watchdog must not fire")
	}
}
