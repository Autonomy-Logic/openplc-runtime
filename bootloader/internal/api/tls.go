// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

package api

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"time"
)

// Self-signed HTTPS cert, generated once into the state dir and persisted
// so the fingerprint stays stable. Owned by the bootloader because
// recovery must work before the runtime has ever started.
const (
	certFileName = "bootloader-cert.pem"
	keyFileName  = "bootloader-key.pem"
	// Ten years. This certificate identifies a device on a plant LAN, and an
	// expiry that stops recovery working on a machine nobody has touched in
	// three years would be a self-inflicted outage.
	certValidity = 10 * 365 * 24 * time.Hour
)

// LoadOrCreateCertificate returns the bootloader's TLS certificate, generating
// and persisting one on first use.
func LoadOrCreateCertificate(stateDir string) (tls.Certificate, error) {
	certPath := filepath.Join(stateDir, certFileName)
	keyPath := filepath.Join(stateDir, keyFileName)

	cert, err := tls.LoadX509KeyPair(certPath, keyPath)
	if err == nil {
		return cert, nil
	}
	if !os.IsNotExist(err) {
		// A present but unreadable or corrupt pair is worth replacing rather
		// than refusing to start: without TLS the bootloader cannot be
		// reached, and being unreachable is the one failure it must not have.
		// The old files are overwritten below.
		if removeErr := os.Remove(certPath); removeErr != nil && !os.IsNotExist(removeErr) {
			return tls.Certificate{}, fmt.Errorf("replacing unusable certificate: %w", removeErr)
		}
		_ = os.Remove(keyPath)
	}

	if err := generateSelfSigned(certPath, keyPath); err != nil {
		return tls.Certificate{}, err
	}
	cert, err = tls.LoadX509KeyPair(certPath, keyPath)
	if err != nil {
		return tls.Certificate{}, fmt.Errorf("loading freshly generated certificate: %w", err)
	}
	return cert, nil
}

// generateSelfSigned writes a new P-256 ECDSA certificate and key.
// ECDSA because 2048-bit RSA keygen is slow enough on a Pi to notice.
func generateSelfSigned(certPath, keyPath string) error {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return fmt.Errorf("generating key: %w", err)
	}

	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return fmt.Errorf("generating serial: %w", err)
	}

	hostname, err := os.Hostname()
	if err != nil || hostname == "" {
		hostname = "openplc-bootloader"
	}

	template := x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName:   hostname,
			Organization: []string{"OpenPLC Bootloader"},
		},
		NotBefore: time.Now().Add(-time.Hour),
		NotAfter:  time.Now().Add(certValidity),
		KeyUsage:  x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage: []x509.ExtKeyUsage{
			x509.ExtKeyUsageServerAuth,
		},
		BasicConstraintsValid: true,
		IsCA:                  true,
		DNSNames:              []string{hostname, "localhost"},
		// Loopback covers a local probe; the device's LAN addresses are not
		// enumerated because they change with DHCP and a SAN mismatch on a
		// self-signed certificate the client is not verifying anyway would be
		// noise rather than protection.
		IPAddresses: []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")},
	}

	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		return fmt.Errorf("creating certificate: %w", err)
	}

	if err := writePEM(certPath, "CERTIFICATE", der, 0o644); err != nil {
		return err
	}

	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return fmt.Errorf("marshalling key: %w", err)
	}
	// 0600: the private key must not be world-readable even inside a
	// container whose volume an operator may inspect from the host.
	return writePEM(keyPath, "EC PRIVATE KEY", keyDER, 0o600)
}

// writePEM writes a PEM block atomically, so an interrupted first boot cannot
// leave a half-written certificate that fails to parse forever after.
func writePEM(path, blockType string, der []byte, mode os.FileMode) error {
	encoded := pem.EncodeToMemory(&pem.Block{Type: blockType, Bytes: der})
	if encoded == nil {
		return fmt.Errorf("encoding %s for %s", blockType, path)
	}

	tmp, err := os.CreateTemp(filepath.Dir(path), ".pem-*")
	if err != nil {
		return fmt.Errorf("creating temp file for %s: %w", path, err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)

	if _, err := tmp.Write(encoded); err != nil {
		tmp.Close()
		return fmt.Errorf("writing %s: %w", path, err)
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return fmt.Errorf("setting mode on %s: %w", path, err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("closing temp file for %s: %w", path, err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("installing %s: %w", path, err)
	}
	return nil
}
