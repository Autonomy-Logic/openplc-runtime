package runtimeauth

import (
	"crypto/pbkdf2"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"strconv"
	"strings"
)

// Werkzeug's generate_password_hash writes
//
//	pbkdf2:sha256:<iterations>$<salt>$<hex digest>
//
// with the salt used as raw bytes of the ASCII string, not decoded. The runtime
// pins iterations at 600000 (User.derivation_method) but the count is read from
// the stored hash rather than assumed, so a future change on the Python side
// keeps verifying instead of silently rejecting every password.
const (
	pbkdf2Prefix = "pbkdf2:"
	// maxIterations bounds work from a malformed or hostile hash: 600k is the
	// real value, and anything past this would be a denial of service against
	// the recovery component rather than a legitimate cost.
	maxIterations = 5_000_000
)

// ErrUnsupportedHash means the stored hash is not one this code can check. It
// is reported rather than treated as a mismatch so an operator sees "this
// runtime hashes differently" instead of "wrong password".
var ErrUnsupportedHash = errors.New("unsupported password hash format")

// VerifyPassword checks password against a Werkzeug PBKDF2 hash.
//
// The pepper is appended before hashing, exactly as User.set_password does
// (“password = password + PEPPER“). Getting the order wrong would fail every
// login while looking entirely reasonable, which is why the shared test vector
// exists.
func VerifyPassword(storedHash, password, pepper string) (bool, error) {
	if !strings.HasPrefix(storedHash, pbkdf2Prefix) {
		return false, fmt.Errorf("%w: %q", ErrUnsupportedHash, firstField(storedHash))
	}
	method, salt, digest, err := splitHash(storedHash)
	if err != nil {
		return false, err
	}
	algorithm, iterations, err := parseMethod(method)
	if err != nil {
		return false, err
	}

	want, err := hex.DecodeString(digest)
	if err != nil {
		return false, fmt.Errorf("%w: digest is not hex", ErrUnsupportedHash)
	}

	// crypto/pbkdf2 (Go 1.24+). The salt is the raw bytes of the ASCII string
	// Werkzeug stored, not a decoded value -- decoding it would silently
	// derive a different key and fail every login.
	got, err := pbkdf2.Key(algorithm, password+pepper, []byte(salt), iterations, len(want))
	if err != nil {
		return false, fmt.Errorf("deriving key: %w", err)
	}
	// Constant time so a wrong password cannot be distinguished by how long
	// the comparison took.
	return subtle.ConstantTimeCompare(got, want) == 1, nil
}

// splitHash breaks "method$salt$digest" apart. SplitN with 3 so a salt or
// digest containing '$' cannot shift the fields.
func splitHash(storedHash string) (method, salt, digest string, err error) {
	parts := strings.SplitN(storedHash, "$", 3)
	if len(parts) != 3 {
		return "", "", "", fmt.Errorf("%w: expected method$salt$digest", ErrUnsupportedHash)
	}
	return parts[0], parts[1], parts[2], nil
}

// parseMethod reads "pbkdf2:sha256:600000", tolerating the older
// "pbkdf2:sha256" form that Werkzeug wrote with an implicit iteration count.
func parseMethod(method string) (func() hash.Hash, int, error) {
	fields := strings.Split(method, ":")
	if len(fields) < 2 || fields[0] != "pbkdf2" {
		return nil, 0, fmt.Errorf("%w: %q", ErrUnsupportedHash, method)
	}
	if fields[1] != "sha256" {
		// Only sha256 is in use. Naming the digest we found makes a future
		// migration obvious from the log rather than a mystery.
		return nil, 0, fmt.Errorf("%w: pbkdf2 with %s", ErrUnsupportedHash, fields[1])
	}

	iterations := 260000 // Werkzeug's historical default when unstated
	if len(fields) >= 3 {
		parsed, err := strconv.Atoi(fields[2])
		if err != nil || parsed <= 0 {
			return nil, 0, fmt.Errorf("%w: iteration count %q", ErrUnsupportedHash, fields[2])
		}
		iterations = parsed
	}
	if iterations > maxIterations {
		return nil, 0, fmt.Errorf("%w: %d iterations exceeds the cap", ErrUnsupportedHash, iterations)
	}
	return sha256.New, iterations, nil
}

// firstField is the leading colon-separated token, for error messages that
// name the offending scheme without echoing an entire hash into a log.
func firstField(s string) string {
	if idx := strings.IndexAny(s, ":$"); idx >= 0 {
		return s[:idx]
	}
	if len(s) > 16 {
		return s[:16]
	}
	return s
}
