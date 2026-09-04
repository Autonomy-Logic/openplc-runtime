package dockerapi

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// PullProgress is a snapshot of an in-flight image pull.
type PullProgress struct {
	// Phase is the daemon's own wording ("Downloading", "Extracting", ...),
	// passed through rather than translated: it is what a user sees in every
	// other Docker tool, and inventing our own vocabulary would only make the
	// two disagree.
	Phase string
	// Percent is 0-100 across all layers, or nil when the daemon has given no
	// size information to work from -- which happens for layers it already
	// has, and for the brief window before any size is known.
	Percent *int
	// Current and Total are the aggregate byte counts behind Percent.
	Current int64
	Total   int64
}

// pullEvent is one line of the daemon's /images/create stream.
type pullEvent struct {
	Status         string `json:"status"`
	ID             string `json:"id"`
	Error          string `json:"error"`
	ProgressDetail struct {
		Current int64 `json:"current"`
		Total   int64 `json:"total"`
	} `json:"progressDetail"`
	ErrorDetail *struct {
		Message string `json:"message"`
	} `json:"errorDetail"`
}

// StallTimeout is how long a pull may go without any progress before it is
// abandoned.
//
// A stall timeout rather than a total timeout, deliberately. The Docker
// client's streaming pull takes no timeout at all, so a half-open connection
// to the registry leaves the decoder parked forever -- the failure
// orchestrator-agent documents in pull_runtime_image.py, where the entry stuck
// in "pulling" refused every retry for the life of the process. A total
// timeout would instead punish a slow-but-working link, which on a plant
// network is the normal case: a 1.4 GB image over a poor connection can
// legitimately take a very long time while never stalling.
const StallTimeout = 5 * time.Minute

// PullImage pulls ref, reporting progress until it completes.
//
// onProgress is called from the reading goroutine and must not block for long.
// It may be nil.
func (c *Client) PullImage(ctx context.Context, ref string, onProgress func(PullProgress)) error {
	name, tag := splitImageRef(ref)
	params := url.Values{}
	params.Set("fromImage", name)
	params.Set("tag", tag)

	// A cancellable child context so the stall watchdog can abort the read.
	pullCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	body, err := c.stream(pullCtx, http.MethodPost, "/images/create"+encodeQuery(params), nil)
	if err != nil {
		return fmt.Errorf("pulling %s: %w", ref, err)
	}
	defer body.Close()

	watchdog := newStallWatchdog(StallTimeout, cancel)
	defer watchdog.stop()

	// Per-layer byte counts, so the aggregate percentage is over the whole
	// image rather than whichever layer reported last.
	layers := map[string]struct{ current, total int64 }{}

	decoder := json.NewDecoder(body)
	for {
		var event pullEvent
		if err := decoder.Decode(&event); err != nil {
			if err == io.EOF {
				return nil
			}
			// Distinguish our own stall abort from a genuine transport error:
			// "context canceled" on its own would send an operator looking for
			// a network fault that did not happen.
			if watchdog.fired() {
				return fmt.Errorf(
					"pulling %s: no progress for %s, giving up", ref, StallTimeout)
			}
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return fmt.Errorf("pulling %s: %w", ref, err)
		}
		watchdog.beat()

		// The daemon reports failures inside the stream with a 200 status, so
		// this is the only place a bad tag or an auth problem surfaces.
		if event.Error != "" {
			return fmt.Errorf("pulling %s: %s", ref, event.Error)
		}
		if event.ErrorDetail != nil && event.ErrorDetail.Message != "" {
			return fmt.Errorf("pulling %s: %s", ref, event.ErrorDetail.Message)
		}

		if onProgress == nil {
			continue
		}
		if event.ID != "" && event.ProgressDetail.Total > 0 {
			layers[event.ID] = struct{ current, total int64 }{
				current: event.ProgressDetail.Current,
				total:   event.ProgressDetail.Total,
			}
		}
		onProgress(aggregate(event.Status, layers))
	}
}

// aggregate sums the per-layer counters into one progress report.
func aggregate(phase string, layers map[string]struct{ current, total int64 }) PullProgress {
	var current, total int64
	for _, layer := range layers {
		current += layer.current
		total += layer.total
	}
	progress := PullProgress{Phase: phase, Current: current, Total: total}
	if total > 0 {
		percent := int(current * 100 / total)
		// Clamp: the daemon occasionally reports current slightly above total
		// for a layer, and a progress bar reading 103% looks like a bug.
		if percent > 100 {
			percent = 100
		}
		if percent < 0 {
			percent = 0
		}
		progress.Percent = &percent
	}
	return progress
}

// stallWatchdog cancels a context when beat() has not been called for the
// configured duration.
type stallWatchdog struct {
	mu       sync.Mutex
	timer    *time.Timer
	timeout  time.Duration
	tripped  bool
	stopped  bool
	onExpire func()
}

func newStallWatchdog(timeout time.Duration, onExpire func()) *stallWatchdog {
	w := &stallWatchdog{timeout: timeout, onExpire: onExpire}
	w.timer = time.AfterFunc(timeout, w.expire)
	return w
}

func (w *stallWatchdog) expire() {
	w.mu.Lock()
	if w.stopped {
		w.mu.Unlock()
		return
	}
	w.tripped = true
	w.mu.Unlock()
	w.onExpire()
}

func (w *stallWatchdog) beat() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.stopped || w.tripped {
		return
	}
	w.timer.Reset(w.timeout)
}

func (w *stallWatchdog) fired() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.tripped
}

func (w *stallWatchdog) stop() {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.stopped = true
	w.timer.Stop()
}

// ImageInfo is the subset of an image inspect the bootloader uses.
type ImageInfo struct {
	ID       string   `json:"Id"`
	RepoTags []string `json:"RepoTags"`
	Size     int64    `json:"Size"`
}

// InspectImage reports whether an image is present locally, and its size.
// A missing image yields an error satisfying IsNotFound.
func (c *Client) InspectImage(ctx context.Context, ref string) (*ImageInfo, error) {
	var out ImageInfo
	path := "/images/" + url.PathEscape(ref) + "/json"
	if err := c.do(ctx, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// RemoveImage deletes an image by reference.
//
// A missing image is success: the goal is "not present". A conflict is NOT
// swallowed -- it means a container still references the image, and silently
// ignoring that would leave an operator believing disk was reclaimed when it
// was not.
func (c *Client) RemoveImage(ctx context.Context, ref string, force bool) error {
	params := url.Values{}
	if force {
		params.Set("force", "true")
	}
	path := "/images/" + url.PathEscape(ref) + encodeQuery(params)
	if err := c.do(ctx, http.MethodDelete, path, nil, nil); err != nil && !IsNotFound(err) {
		return err
	}
	return nil
}

// splitImageRef separates a reference into name and tag.
//
// Only the last colon counts as the tag separator, and only when it appears
// after the final slash: a registry with a port ("host:5000/image") puts a
// colon in the name, and splitting on the first would produce nonsense.
func splitImageRef(ref string) (name, tag string) {
	lastSlash := strings.LastIndex(ref, "/")
	lastColon := strings.LastIndex(ref, ":")
	if lastColon > lastSlash {
		return ref[:lastColon], ref[lastColon+1:]
	}
	// An untagged reference means latest, the same default docker itself uses.
	return ref, "latest"
}
