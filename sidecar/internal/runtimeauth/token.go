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

// Tokens are HS256 JWTs interchangeable with the runtime's.
//
// Both sides sign with JWT_SECRET_KEY from the shared .env, so a token minted
// by either is accepted by both. That matters in one direction in particular:
// a token issued during recovery, when the runtime was down, keeps working
// against the runtime once it comes back, so the editor is not forced to log
// in twice around an update.
//
// Only the claims flask_jwt_extended actually checks are produced -- "sub",
// "type", "iat", "nbf", "exp" and "jti". A hand-rolled implementation rather
// than a JWT library because HS256 is an HMAC over two base64url segments, and
// the library-shaped risk here (accepting "alg": "none", or letting the token
// choose its own algorithm) is precisely what an explicit implementation
// avoids: the algorithm below is a constant, never read from the header.
const (
	// TokenType is flask_jwt_extended's discriminator. A refresh token
	// presented as an access token must not be accepted.
	TokenType = "access"
	// DefaultTokenTTL is deliberately longer than the runtime's 15-minute
	// default: a version change involves an image pull that can run for many
	// minutes on a plant link, and having the caller's token expire midway
	// through would strand a device mid-update.
	DefaultTokenTTL = 2 * time.Hour
	// clockSkew tolerates a small disagreement between the editor's clock and
	// the device's, which on an industrial box without NTP is routine.
	clockSkew = 60 * time.Second
)

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

// Claims is the payload the sidecar reads and writes.
type Claims struct {
	Subject   string `json:"sub"`
	Type      string `json:"type"`
	IssuedAt  int64  `json:"iat"`
	NotBefore int64  `json:"nbf"`
	Expires   int64  `json:"exp"`
	JTI       string `json:"jti"`
}

type jwtHeader struct {
	Alg string `json:"alg"`
	Typ string `json:"typ"`
}

// IssueToken mints an access token for the given user id.
//
// The subject is the user's numeric id rendered as a string, matching the
// runtime's user_identity_lookup (“return str(user.id)“). A username here
// would produce a token the runtime accepts structurally but then fails to
// resolve to a user, which is a confusing way to be broken.
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

	// The algorithm is NOT taken from the header. Trusting the header is the
	// classic JWT vulnerability: a token claiming "alg": "none" or "HS256"
	// against an RSA key gets verified against attacker-chosen rules. Here
	// HS256 is the only thing that is ever computed, so a header saying
	// otherwise simply fails the comparison below.
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

func sign(secret, signingInput string) string {
	mac := hmac.New(sha256.New, []byte(secret))
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
