// Command stubruntime stands in for the OpenPLC runtime in integration tests.
//
// It serves the two endpoints the bootloader actually depends on -- an
// unauthenticated /api/version and a healthcheck -- and nothing else. The
// point is not to emulate the runtime; it is to make the runtime's FAILURE
// modes reproducible on demand, which the real image cannot be asked to do.
// A real runtime cannot be told "exit 1 during start-up" or "come up healthy
// then die three times", and those are exactly the paths where the
// bootloader's crash accounting and recovery transitions live.
//
// The real image is exercised separately in the same harness for the
// does-it-actually-come-up case, and hardware behaviour (SPI, GPIO, VPP
// plugins, real SCHED_FIFO latency) is validated on a device, which no
// container on a developer machine can stand in for.
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"flag"
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"strconv"
	"time"
)

const listenAddr = ":8443"

func main() {
	probe := flag.Bool("probe", false, "probe the local endpoint and exit (used as HEALTHCHECK)")
	flag.Parse()

	if *probe {
		os.Exit(runProbe())
	}

	version := envOr("STUB_VERSION", "v0.0.0-stub")
	failMode := os.Getenv("STUB_FAIL")

	switch failMode {
	case "exit":
		// A runtime whose image is broken: it dies during start-up and never
		// serves anything. Drives the "did not start" path.
		log.Printf("stub %s: STUB_FAIL=exit, exiting 1 immediately", version)
		os.Exit(1)
	case "hang":
		// Alive but never answering. This is the case Docker's events stream
		// cannot report on its own -- no die event is ever emitted -- so the
		// healthcheck is the only thing that notices.
		log.Printf("stub %s: STUB_FAIL=hang, listening on nothing", version)
		select {}
	}

	if failMode == "crash-loop" {
		after := envDuration("STUB_CRASH_AFTER", 3*time.Second)
		// Serve first, THEN die. This is the shape that matters: a program
		// that faults on load lets the webserver come up before it takes the
		// process down, which is why a healthy start must not clear the
		// bootloader's crash window.
		go func() {
			time.Sleep(after)
			log.Printf("stub %s: crash-loop mode, exiting 1 after %s", version, after)
			os.Exit(1)
		}()
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/version", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-OpenPLC-Runtime-Version", version)
		fmt.Fprintf(w, `{"version":%q}`, version)
	})
	mux.HandleFunc("/api/capabilities", func(w http.ResponseWriter, r *http.Request) {
		policy := envOr("OPENPLC_UPDATE_POLICY", "manual")
		port := envOr("OPENPLC_BOOTLOADER_PORT", "null")
		dataDir := os.Getenv("OPENPLC_PERSISTENT_DATA_DIR")
		w.Header().Set("Content-Type", "application/json")
		// dataDir is echoed so a test can assert the bootloader passed it --
		// the bug where the runtime ignored the mounted directory was invisible
		// from outside until something reported what it had been told.
		fmt.Fprintf(w,
			`{"runtimeVersion":%q,"updatePolicy":%q,"bootloaderPort":%s,"dataDir":%q}`,
			version, policy, port, dataDir)
	})

	cert, err := selfSigned()
	if err != nil {
		log.Fatalf("stub: generating certificate: %v", err)
	}
	server := &http.Server{
		Addr:              listenAddr,
		Handler:           mux,
		TLSConfig:         &tls.Config{Certificates: []tls.Certificate{cert}},
		ReadHeaderTimeout: 5 * time.Second,
	}

	log.Printf("stub %s: listening on %s (fail=%q)", version, listenAddr, failMode)
	if err := server.ListenAndServeTLS("", ""); err != nil {
		log.Fatalf("stub: %v", err)
	}
}

// runProbe is the HEALTHCHECK. It lives in the same binary so the image needs
// no shell and no curl, which keeps it on scratch.
func runProbe() int {
	client := &http.Client{
		Timeout: 3 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // loopback
		},
	}
	resp, err := client.Get("https://127.0.0.1:8443/api/version")
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func selfSigned() (tls.Certificate, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, err
	}
	template := x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "stubruntime"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * 365 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		DNSNames:              []string{"localhost"},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, err
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, nil
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func envDuration(name string, fallback time.Duration) time.Duration {
	raw := os.Getenv(name)
	if raw == "" {
		return fallback
	}
	seconds, err := strconv.Atoi(raw)
	if err != nil || seconds <= 0 {
		return fallback
	}
	return time.Duration(seconds) * time.Second
}
