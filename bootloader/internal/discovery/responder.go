// Package discovery answers the editor's LAN discovery probe while the runtime
// is not running.
//
// The runtime has its own responder (webserver/discovery/network_discovery.py)
// and normally owns this port. The bootloader's exists for one situation: the
// runtime is down, so nothing is answering, and a device that cannot be found
// cannot be repaired. Without this, a failed update makes a device vanish from
// the editor's list at exactly the moment somebody needs to reach it.
//
// It runs ONLY in recovery mode, which is what keeps the two responders from
// ever competing. Recovery is defined as "the runtime container is stopped" --
// the supervisor stops it before entering that state -- so exclusivity holds
// by construction rather than by coordination. Two services answering the same
// broadcast would give the editor two different answers for one device.
//
// The protocol is the runtime's, byte for byte: a fixed magic string in, one
// JSON datagram back, unicast to the sender.
package discovery

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"os"
	"strconv"
	"sync"
	"time"
)

// Wire constants, mirrored from webserver/discovery/network_discovery.py.
// They must match exactly or the editor will not see the device at all.
const (
	Port           = 33333
	Magic          = "OPENPLC_DISCOVER_V1"
	ProtocolVer    = 1
	RuntimeAPIPort = 8443

	// maxRequestBytes drops oversized packets without parsing them. The magic
	// string is 19 bytes; the slack is for a future protocol revision, not for
	// inviting amplification probes.
	maxRequestBytes = 64

	// perIPRateLimit matches the runtime's. Discovery is a user-driven action,
	// so a generous floor still feels instant while a spamming probe gets
	// dropped.
	perIPRateLimit = 100 * time.Millisecond
)

// Reply is what a probing editor receives.
//
// service says "openplc-bootloader", not "openplc-runtime". Being honest here
// costs an older editor the ability to see a device in recovery -- but an
// older editor could not have done anything about it either, and the
// alternative is a client that thinks it is talking to a working runtime and
// then fails against every endpoint it tries.
type Reply struct {
	Service         string `json:"service"`
	ProtocolVersion int    `json:"protocol_version"`
	Hostname        string `json:"hostname"`
	// Recovery is always true: this responder only runs in recovery mode.
	// It is stated explicitly so a client keys off a field rather than
	// inferring meaning from the service name.
	Recovery bool `json:"recovery"`
	// BootloaderPort is where to go next. The whole reply exists to hand the
	// editor this number.
	BootloaderPort int `json:"bootloader_port"`
	// RuntimeVersion is the version the device INTENDS to run, which is not
	// running right now. Shown so an operator can see what was attempted.
	RuntimeVersion string `json:"runtime_version,omitempty"`
	// Reason is the supervisor's own wording, so the device list can say why
	// without anyone having to log in first.
	Reason string `json:"reason,omitempty"`
	// APIPort is included for symmetry with the runtime's reply; nothing is
	// listening on it in recovery.
	APIPort int `json:"api_port"`
}

// ReplyProvider supplies the current answer. A function rather than a struct
// so the responder never holds stale state: recovery reasons change, and a
// cached reason is worse than none.
type ReplyProvider func() Reply

// Responder answers discovery probes while enabled.
type Responder struct {
	provider ReplyProvider
	log      *slog.Logger
	port     int

	mu       sync.Mutex
	conn     *net.UDPConn
	lastSeen map[string]time.Time
}

// New builds a Responder. It is not listening until Enable.
func New(port int, provider ReplyProvider, log *slog.Logger) *Responder {
	if port == 0 {
		port = Port
	}
	return &Responder{
		provider: provider,
		log:      log,
		port:     port,
		lastSeen: map[string]time.Time{},
	}
}

// Enable starts answering probes. Safe to call when already enabled.
//
// A bind failure is logged and swallowed. Discovery is a convenience: losing
// it must not stop the bootloader serving its control API, which is the
// primary way in. The most likely cause is the runtime still holding the port,
// and in that case the device is findable anyway.
func (r *Responder) Enable() {
	r.mu.Lock()
	if r.conn != nil {
		r.mu.Unlock()
		return
	}
	var conn *net.UDPConn
	// SO_REUSEADDR and SO_REUSEPORT, matching how the runtime binds the same
	// port. Linux shares a UDP port only when EVERY socket asked to, so
	// without these a lingering bootloader socket makes the runtime's own bind
	// fail -- and the runtime does not retry. The release now happens before
	// the runtime starts; this is the safety net for a race in between.
	listener := net.ListenConfig{Control: reusePort}
	generic, err := listener.ListenPacket(
		context.Background(), "udp", ":"+strconv.Itoa(r.port))
	if err == nil {
		var ok bool
		if conn, ok = generic.(*net.UDPConn); !ok {
			generic.Close()
			err = errors.New("discovery: listener is not a UDP socket")
		}
	}
	if err != nil {
		r.mu.Unlock()
		r.log.Warn("discovery responder could not bind; the device will not "+
			"answer LAN discovery while in recovery",
			"port", r.port, "error", err)
		return
	}
	r.conn = conn
	r.mu.Unlock()

	r.log.Info("discovery responder enabled", "port", r.port)
	go r.serve(conn)
}

// Disable stops answering. Called when the runtime comes back healthy, so the
// runtime's own responder can have the port again.
func (r *Responder) Disable() {
	r.mu.Lock()
	conn := r.conn
	r.conn = nil
	r.lastSeen = map[string]time.Time{}
	r.mu.Unlock()

	if conn == nil {
		return
	}
	// Closing is what unblocks the ReadFromUDP in serve.
	if err := conn.Close(); err != nil {
		r.log.Debug("closing the discovery socket", "error", err)
	}
	r.log.Info("discovery responder disabled")
}

// Enabled reports whether the responder is listening.
func (r *Responder) Enabled() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.conn != nil
}

func (r *Responder) serve(conn *net.UDPConn) {
	buf := make([]byte, maxRequestBytes+1)
	for {
		n, addr, err := conn.ReadFromUDP(buf)
		if err != nil {
			// A closed socket is the normal way this loop ends, via Disable.
			if errors.Is(err, net.ErrClosed) {
				return
			}
			r.log.Debug("discovery read", "error", err)
			continue
		}
		// Oversized, or not the magic string: dropped in silence, exactly as
		// the runtime does. Answering unknown payloads would make this an
		// amplification target.
		if n > maxRequestBytes || string(buf[:n]) != Magic {
			continue
		}
		if !r.allow(addr) {
			continue
		}
		r.respond(conn, addr)
	}
}

// allow applies the per-source-IP rate limit.
func (r *Responder) allow(addr *net.UDPAddr) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	key := addr.IP.String()
	now := time.Now()
	if last, seen := r.lastSeen[key]; seen && now.Sub(last) < perIPRateLimit {
		return false
	}
	r.lastSeen[key] = now
	// Garbage-collect so a long-running bootloader does not accumulate state
	// from drive-by probes, matching the runtime's own bound.
	if len(r.lastSeen) > 1024 {
		cutoff := now.Add(-time.Minute)
		for ip, ts := range r.lastSeen {
			if ts.Before(cutoff) {
				delete(r.lastSeen, ip)
			}
		}
	}
	return true
}

func (r *Responder) respond(conn *net.UDPConn, addr *net.UDPAddr) {
	reply := r.provider()
	reply.Service = "openplc-bootloader"
	reply.ProtocolVersion = ProtocolVer
	reply.Recovery = true
	reply.APIPort = RuntimeAPIPort
	if reply.Hostname == "" {
		if hostname, err := os.Hostname(); err == nil {
			reply.Hostname = hostname
		}
	}

	payload, err := json.Marshal(reply)
	if err != nil {
		r.log.Warn("encoding a discovery reply", "error", err)
		return
	}
	// Unicast back to the sender: that is the authoritative way for the
	// editor to learn a reachable address, since a multi-homed device does
	// not reliably know its own outward-facing IP.
	if _, err := conn.WriteToUDP(payload, addr); err != nil {
		r.log.Debug("discovery reply", "to", addr.String(), "error", err)
	}
}
