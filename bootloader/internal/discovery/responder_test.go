package discovery

import (
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"testing"
	"time"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// freePort picks an unused UDP port so tests never collide with the real
// 33333, which a developer machine may well have something on.
func freePort(t *testing.T) int {
	t.Helper()
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("reserving a port: %v", err)
	}
	port := conn.LocalAddr().(*net.UDPAddr).Port
	conn.Close()
	return port
}

// probe sends payload and returns the reply, or nil when nothing answered.
func probe(t *testing.T, port int, payload string) []byte {
	t.Helper()
	conn, err := net.DialUDP("udp", nil, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port})
	if err != nil {
		t.Fatalf("dialling: %v", err)
	}
	defer conn.Close()

	if _, err := conn.Write([]byte(payload)); err != nil {
		t.Fatalf("writing probe: %v", err)
	}
	if err := conn.SetReadDeadline(time.Now().Add(500 * time.Millisecond)); err != nil {
		t.Fatalf("setting deadline: %v", err)
	}
	buf := make([]byte, 2048)
	n, err := conn.Read(buf)
	if err != nil {
		return nil
	}
	return buf[:n]
}

func newTestResponder(t *testing.T, reply Reply) (*Responder, int) {
	t.Helper()
	port := freePort(t)
	r := New(port, func() Reply { return reply }, quietLogger())
	t.Cleanup(r.Disable)
	return r, port
}

func TestTheResponderIsSilentUntilEnabled(t *testing.T) {
	// It must never answer outside recovery. The runtime owns this port
	// normally, and two services answering one broadcast would give the editor
	// two different answers for the same device.
	_, port := newTestResponder(t, Reply{})
	if got := probe(t, port, Magic); got != nil {
		t.Fatalf("a disabled responder must not answer, got %q", got)
	}
}

func TestAnEnabledResponderAnswersTheMagicString(t *testing.T) {
	r, port := newTestResponder(t, Reply{
		RuntimeVersion: "v4.2.1",
		Reason:         "runtime exited 3 times within 5m0s",
		BootloaderPort: 8445,
	})
	r.Enable()

	raw := probe(t, port, Magic)
	if raw == nil {
		t.Fatal("no reply to the discovery magic")
	}
	var reply Reply
	if err := json.Unmarshal(raw, &reply); err != nil {
		t.Fatalf("reply is not JSON: %v (%q)", err, raw)
	}

	if reply.Service != "openplc-bootloader" {
		t.Errorf("the reply must identify the bootloader, got %q", reply.Service)
	}
	if !reply.Recovery {
		t.Error("recovery must be stated explicitly, not inferred from the service name")
	}
	if reply.BootloaderPort != 8445 {
		t.Errorf("the reply exists to hand over this port, got %d", reply.BootloaderPort)
	}
	if reply.ProtocolVersion != ProtocolVer {
		t.Errorf("want protocol version %d, got %d", ProtocolVer, reply.ProtocolVersion)
	}
	if reply.Hostname == "" {
		t.Error("the hostname must be filled in so a device list can name the device")
	}
	if reply.Reason == "" {
		t.Error("the reason must travel with the reply, so a device list can say " +
			"why without anyone logging in")
	}
}

func TestAnythingOtherThanTheMagicStringIsDropped(t *testing.T) {
	// Answering unknown payloads would make this an amplification target.
	r, port := newTestResponder(t, Reply{})
	r.Enable()

	for _, payload := range []string{
		"",
		"hello",
		"OPENPLC_DISCOVER_V2",
		"openplc_discover_v1", // case matters
		Magic + "x",
	} {
		if got := probe(t, port, payload); got != nil {
			t.Errorf("payload %q must be dropped, got a reply %q", payload, got)
		}
	}
}

func TestAnOversizedPacketIsDroppedWithoutParsing(t *testing.T) {
	r, port := newTestResponder(t, Reply{})
	r.Enable()

	oversized := Magic
	for len(oversized) <= maxRequestBytes {
		oversized += "A"
	}
	if got := probe(t, port, oversized); got != nil {
		t.Fatalf("an oversized packet must be dropped, got %q", got)
	}
}

func TestDisablingStopsTheResponder(t *testing.T) {
	// The runtime's own responder needs the port back once it is healthy.
	r, port := newTestResponder(t, Reply{})
	r.Enable()
	if probe(t, port, Magic) == nil {
		t.Fatal("expected a reply while enabled")
	}

	r.Disable()
	if r.Enabled() {
		t.Fatal("Enabled() must report the responder as off")
	}
	if got := probe(t, port, Magic); got != nil {
		t.Fatalf("a disabled responder must not answer, got %q", got)
	}
}

func TestEnableIsIdempotent(t *testing.T) {
	// The supervisor's transition hooks can fire more than once for the same
	// state; a second Enable must not fail or leak a second socket.
	r, port := newTestResponder(t, Reply{})
	r.Enable()
	r.Enable()
	if probe(t, port, Magic) == nil {
		t.Fatal("expected a reply after a repeated Enable")
	}
}

func TestTheResponderCanBeCycled(t *testing.T) {
	// A device can go healthy, fail again, and need discovery a second time.
	// Re-binding the same port after a close is the part that breaks if the
	// socket was not released properly.
	r, port := newTestResponder(t, Reply{})
	for i := 0; i < 3; i++ {
		r.Enable()
		if probe(t, port, Magic) == nil {
			t.Fatalf("cycle %d: expected a reply", i)
		}
		r.Disable()
		if got := probe(t, port, Magic); got != nil {
			t.Fatalf("cycle %d: expected silence, got %q", i, got)
		}
	}
}

func TestTheRateLimitDropsARepeatedProbe(t *testing.T) {
	r, port := newTestResponder(t, Reply{})
	r.Enable()

	if probe(t, port, Magic) == nil {
		t.Fatal("the first probe must be answered")
	}
	// Immediately again from the same source: inside the window, so dropped.
	if got := probe(t, port, Magic); got != nil {
		t.Fatalf("a probe inside the rate-limit window must be dropped, got %q", got)
	}
	// And allowed again once the window passes, or a user pressing "scan"
	// twice would think the device had disappeared.
	time.Sleep(perIPRateLimit + 50*time.Millisecond)
	if probe(t, port, Magic) == nil {
		t.Fatal("a probe after the window must be answered again")
	}
}

func TestTheWireConstantsMatchTheRuntime(t *testing.T) {
	// These are mirrored from webserver/discovery/network_discovery.py. If
	// they drift, the editor simply will not see a device in recovery -- a
	// silent failure, so it is pinned here.
	if Port != 33333 {
		t.Errorf("discovery port must be 33333, got %d", Port)
	}
	if Magic != "OPENPLC_DISCOVER_V1" {
		t.Errorf("magic string must match the runtime's, got %q", Magic)
	}
	if ProtocolVer != 1 {
		t.Errorf("protocol version must be 1, got %d", ProtocolVer)
	}
	if maxRequestBytes != 64 {
		t.Errorf("request cap must match the runtime's 64, got %d", maxRequestBytes)
	}
	if perIPRateLimit != 100*time.Millisecond {
		t.Errorf("rate limit must match the runtime's 0.1s, got %s", perIPRateLimit)
	}
}
