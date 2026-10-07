// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

package dockerapi

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// Event is one entry from GET /events, narrowed to what the supervisor needs.
type Event struct {
	Type   string `json:"Type"`   // "container", "image", ...
	Action string `json:"Action"` // "die", "start", "health_status: unhealthy", ...
	Actor  struct {
		ID         string            `json:"ID"`
		Attributes map[string]string `json:"Attributes"`
	} `json:"Actor"`
	TimeNano int64 `json:"timeNano"`
}

// ContainerName returns the container name the event concerns, which the
// daemon supplies as an actor attribute rather than a top-level field.
func (e *Event) ContainerName() string {
	return e.Actor.Attributes["name"]
}

// ExitCode returns the exit code carried by a "die" event. Present only on
// die; the second return reports whether it was there at all, because exit 0
// and "no exit code" mean very different things to crash-loop accounting.
func (e *Event) ExitCode() (int, bool) {
	raw, ok := e.Actor.Attributes["exitCode"]
	if !ok {
		return 0, false
	}
	var code int
	if _, err := fmt.Sscanf(raw, "%d", &code); err != nil {
		return 0, false
	}
	return code, true
}

// HealthStatus returns the verdict from a "health_status: X" action, or "".
// The daemon encodes it in the action string rather than an attribute.
func (e *Event) HealthStatus() string {
	const prefix = "health_status: "
	if strings.HasPrefix(e.Action, prefix) {
		return strings.TrimPrefix(e.Action, prefix)
	}
	return ""
}

// StreamEvents delivers container events until ctx is cancelled or the
// stream breaks (always returns non-nil). Caller must reconnect AND
// re-inspect, since state changes during the gap are missed.
func (c *Client) StreamEvents(ctx context.Context, containerName string, handle func(Event)) error {
	filters := map[string][]string{
		"type":      {"container"},
		"container": {containerName},
	}
	encoded, err := json.Marshal(filters)
	if err != nil {
		return fmt.Errorf("encoding event filters: %w", err)
	}
	params := url.Values{}
	params.Set("filters", string(encoded))

	body, err := c.stream(ctx, http.MethodGet, "/events"+encodeQuery(params), nil)
	if err != nil {
		return err
	}
	defer body.Close()

	// The daemon writes one JSON object per line, indefinitely.
	decoder := json.NewDecoder(body)
	for {
		var event Event
		if err := decoder.Decode(&event); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if err == io.EOF {
				return fmt.Errorf("docker events stream closed by daemon")
			}
			return fmt.Errorf("docker events stream: %w", err)
		}
		handle(event)
	}
}

// readMultiplexed decodes Docker's non-TTY framing: 8-byte header
// [stream byte, 3 zero, 4-byte BE length]. Both streams kept in arrival
// order. Output capped at limit bytes, keeping the tail.
func readMultiplexed(r io.Reader, limit int) (string, error) {
	reader := bufio.NewReader(r)
	var out strings.Builder
	header := make([]byte, 8)

	for {
		if _, err := io.ReadFull(reader, header); err != nil {
			if err == io.EOF || err == io.ErrUnexpectedEOF {
				break
			}
			return trimToLimit(out.String(), limit), err
		}
		// A first byte outside the known stream ids means this is not framed
		// output at all (a TTY-allocated container streams raw). Fall back to
		// reading the remainder verbatim rather than emitting garbage.
		if header[0] > 2 {
			out.Write(header)
			rest, err := io.ReadAll(io.LimitReader(reader, int64(limit)))
			out.Write(rest)
			return trimToLimit(out.String(), limit), err
		}
		size := binary.BigEndian.Uint32(header[4:8])
		if size == 0 {
			continue
		}
		// Bound a single frame so a corrupt length cannot allocate wildly.
		if size > uint32(limit) {
			size = uint32(limit)
		}
		frame := make([]byte, size)
		if _, err := io.ReadFull(reader, frame); err != nil {
			out.Write(frame)
			if err == io.EOF || err == io.ErrUnexpectedEOF {
				break
			}
			return trimToLimit(out.String(), limit), err
		}
		out.Write(frame)
		// Keep the builder from growing without bound on a long-lived
		// container: trim as we go, not just at the end.
		if out.Len() > limit*2 {
			trimmed := trimToLimit(out.String(), limit)
			out.Reset()
			out.WriteString(trimmed)
		}
	}
	return trimToLimit(out.String(), limit), nil
}

// trimToLimit keeps the last limit bytes, starting at a line boundary so the
// output never opens mid-line.
func trimToLimit(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	s = s[len(s)-limit:]
	if idx := strings.IndexByte(s, '\n'); idx >= 0 && idx+1 < len(s) {
		return s[idx+1:]
	}
	return s
}
