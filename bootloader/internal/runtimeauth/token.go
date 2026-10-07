// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

package runtimeauth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

// Bootloader HS256 JWTs. Signed with a key DERIVED from JWT_SECRET_KEY
// (see bootloaderKey) so the runtime cannot verify these. Hand-rolled:
// HS256 is a constant, never read from the header.

const (
	// TokenType is flask_jwt_extended's discriminator. A refresh token
	// presented as an access token must not be accepted.
	TokenType = "access"
	// DefaultTokenTTL is longer than the runtime's 15 min default so a
	// slow image pull during an update does not strand the token.
	DefaultTokenTTL = 2 * time.Hour
	// clockSkew tolerates a small disagreement between the editor's clock and
	// the device's, which on an industrial box without NTP is routine.
	clockSkew = 60 * time.Second
	// Audience names the only service that may accept these tokens.
	Audience = "openplc-bootloader"
	// keyDomain separates the bootloader's signing key from the runtime's.
	// Versioned so a future change of scheme can be told apart from this one.
	keyDomain = "openplc-bootloader/token/v1"
)

// bootloaderKey derives the bootloader signing key from the runtime
// secret via HMAC with a fixed domain string. One-way, so the runtime
// cannot compute it and cannot verify bootloader tokens.
func bootloaderKey(secret string) []byte {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(keyDomain))
	return mac.Sum(nil)
}

var (
	// ErrInvalidToken covers every rejection reason. The cause is logged but
	// never returned to the caller: telling an unauthenticated client whether
	// a token was expired, mis-signed or malformed is free reconnaissance.
	ErrInvalidToken = errors.New("invalid token")
	// ErrTokenExpired is separated ONLY so the API can answer 401 with a hint
	// that re-authenticating will help, which is genuinely useful and reveals
	// nothing an attacker could not learn by waiting.
	ErrTokenExpired = errors.New("token expired")
)

// Claims is the payload the bootloader reads and writes.
type Claims struct {
	Subject   string `json:"sub"`
	Type      string `json:"type"`
	IssuedAt  int64  `json:"iat"`
	NotBefore int64  `json:"nbf"`
	Expires   int64  `json:"exp"`
	JTI       string `json:"jti"`
	Audience  string `json:"aud,omitempty"`
}

type jwtHeader struct {
	Alg string `json:"alg"`
	Typ string `json:"typ"`
}

// IssueToken mints an access token for the given user id. Subject is
// the user's numeric id as a string, matching user_identity_lookup.
func IssueToken(secret, userID string, ttl time.Duration) (string, error) {
	if secret == "" {
		return "", errors.New("cannot issue a token without a signing secret")
	}
	if ttl <= 0 {
		ttl = DefaultTokenTTL
	}
	jti, err := randomJTI()
	if err != nil {
		return "", err
	}

	now := time.Now().UTC()
	claims := Claims{
		Subject:   userID,
		Type:      TokenType,
		IssuedAt:  now.Unix(),
		NotBefore: now.Unix(),
		Expires:   now.Add(ttl).Unix(),
		JTI:       jti,
		Audience:  Audience,
	}

	header, err := json.Marshal(jwtHeader{Alg: "HS256", Typ: "JWT"})
	if err != nil {
		return "", fmt.Errorf("encoding token header: %w", err)
	}
	payload, err := json.Marshal(claims)
	if err != nil {
		return "", fmt.Errorf("encoding token claims: %w", err)
	}

	signingInput := encodeSegment(header) + "." + encodeSegment(payload)
	return signingInput + "." + sign(secret, signingInput), nil
}

// VerifyToken checks a token's signature and time claims and returns them.
func VerifyToken(secret, token string) (*Claims, error) {
	if secret == "" {
		return nil, ErrInvalidToken
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, ErrInvalidToken
	}
	signingInput := parts[0] + "." + parts[1]

	// Algorithm is a constant (HS256), never read from the header, so a
	// token claiming "alg": "none" fails on the signature comparison.
	expected := sign(secret, signingInput)
	if subtle.ConstantTimeCompare([]byte(expected), []byte(parts[2])) != 1 {
		return nil, ErrInvalidToken
	}

	raw, err := decodeSegment(parts[1])
	if err != nil {
		return nil, ErrInvalidToken
	}
	var claims Claims
	if err := json.Unmarshal(raw, &claims); err != nil {
		return nil, ErrInvalidToken
	}

	if claims.Type != TokenType {
		return nil, ErrInvalidToken
	}
	// A token minted for anything but this service is refused even if it
	// somehow verified. The derived signing key already makes a runtime token
	// fail above; this catches the case where a future change reunifies the
	// keys without anyone noticing.
	if claims.Audience != Audience {
		return nil, ErrInvalidToken
	}
	if claims.Subject == "" {
		return nil, ErrInvalidToken
	}

	now := time.Now().UTC()
	if claims.Expires > 0 && now.After(time.Unix(claims.Expires, 0).Add(clockSkew)) {
		return nil, ErrTokenExpired
	}
	if claims.NotBefore > 0 && now.Add(clockSkew).Before(time.Unix(claims.NotBefore, 0)) {
		return nil, ErrInvalidToken
	}
	return &claims, nil
}

// sign computes the signature with the DERIVED key, never the runtime's
// secret itself. That single substitution is what keeps the two token spaces
// apart in both directions.
func sign(secret, signingInput string) string {
	mac := hmac.New(sha256.New, bootloaderKey(secret))
	mac.Write([]byte(signingInput))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// encodeSegment is base64url without padding, as JWS requires.
func encodeSegment(raw []byte) string {
	return base64.RawURLEncoding.EncodeToString(raw)
}

// decodeSegment accepts padded input too: PyJWT emits unpadded, but a
// hand-assembled token from a test or another client may not, and rejecting a
// structurally valid token over padding would be a pointless
// incompatibility.
func decodeSegment(segment string) ([]byte, error) {
	if decoded, err := base64.RawURLEncoding.DecodeString(segment); err == nil {
		return decoded, nil
	}
	return base64.URLEncoding.DecodeString(segment)
}

func randomJTI() (string, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("generating token id: %w", err)
	}
	return hex.EncodeToString(buf), nil
}
