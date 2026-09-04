package api

import (
	"crypto/x509"
	"os"
	"path/filepath"
	"testing"
)

func TestACertificateIsGeneratedOnFirstUse(t *testing.T) {
	dir := t.TempDir()
	cert, err := LoadOrCreateCertificate(dir)
	if err != nil {
		t.Fatalf("first use: %v", err)
	}
	if len(cert.Certificate) == 0 {
		t.Fatal("no certificate returned")
	}
	for _, name := range []string{certFileName, keyFileName} {
		if _, err := os.Stat(filepath.Join(dir, name)); err != nil {
			t.Fatalf("%s was not persisted: %v", name, err)
		}
	}
}

func TestTheCertificateIsReusedOnSubsequentBoots(t *testing.T) {
	// Regenerating on every boot would change the fingerprint each time the
	// device restarted, which trains operators to click through certificate
	// warnings -- the opposite of what a certificate is for.
	dir := t.TempDir()
	first, err := LoadOrCreateCertificate(dir)
	if err != nil {
		t.Fatalf("first use: %v", err)
	}
	second, err := LoadOrCreateCertificate(dir)
	if err != nil {
		t.Fatalf("second use: %v", err)
	}
	if string(first.Certificate[0]) != string(second.Certificate[0]) {
		t.Fatal("the certificate must be stable across boots")
	}
}

func TestACorruptCertificateIsReplacedRatherThanFatal(t *testing.T) {
	// Being unreachable is the one failure the bootloader must not have, so a
	// damaged key pair is worth replacing rather than refusing to start over.
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, certFileName), []byte("not a pem"), 0o644); err != nil {
		t.Fatalf("seeding corrupt cert: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, keyFileName), []byte("nor this"), 0o600); err != nil {
		t.Fatalf("seeding corrupt key: %v", err)
	}

	cert, err := LoadOrCreateCertificate(dir)
	if err != nil {
		t.Fatalf("a corrupt pair must be replaced, got: %v", err)
	}
	if len(cert.Certificate) == 0 {
		t.Fatal("no certificate returned")
	}
}

func TestThePrivateKeyIsNotWorldReadable(t *testing.T) {
	dir := t.TempDir()
	if _, err := LoadOrCreateCertificate(dir); err != nil {
		t.Fatalf("generating: %v", err)
	}
	info, err := os.Stat(filepath.Join(dir, keyFileName))
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if mode := info.Mode().Perm(); mode&0o077 != 0 {
		t.Fatalf("key mode %04o is readable beyond its owner", mode)
	}
}

func TestTheCertificateIsUsableForServerAuth(t *testing.T) {
	dir := t.TempDir()
	cert, err := LoadOrCreateCertificate(dir)
	if err != nil {
		t.Fatalf("generating: %v", err)
	}
	parsed, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		t.Fatalf("parsing: %v", err)
	}

	var serverAuth bool
	for _, usage := range parsed.ExtKeyUsage {
		if usage == x509.ExtKeyUsageServerAuth {
			serverAuth = true
		}
	}
	if !serverAuth {
		t.Error("the certificate must be valid for server authentication")
	}
	// A certificate that expires would stop recovery working on a device
	// nobody has touched in years.
	if parsed.NotAfter.Sub(parsed.NotBefore) < 5*365*24*3600*1e9 {
		t.Errorf("validity is too short: %s", parsed.NotAfter.Sub(parsed.NotBefore))
	}
}
