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

// Tokens are HS256 JWTs in the same shape as the runtime's, but they are NOT
// the runtime's tokens and neither service will accept the other's.
//
// What the two share is the credential database, not a session: the editor
// keeps the user's credentials after login and signs in to the bootloader
// separately when it needs to.
//
// Separation is enforced by the signing key, not by a claim. Both services
// read the same JWT_SECRET_KEY from the same .env, so signing with it
// directly made the two token spaces identical: a 2-hour bootloader token was
// a valid runtime token, eight times the runtime's own 15-minute TTL, and the
// runtime's /logout revoked neither. The bootloader therefore signs with a key
// DERIVED from that secret (see bootloaderKey), which the runtime does not
// know how to compute. A runtime token fails the signature check here, and a
// bootloader token fails it there -- with no change required in the runtime,
// and no reliance on a verifier bothering to check an audience claim.
//
// The `aud` claim below is belt and braces: it makes the intent legible in a
// decoded token and would catch a future signing change that reunified the
// keys by accident.
//
// The rest of the claim set still mirrors flask_jwt_extended's -- "sub",
// "type", "iat", "nbf", "exp", "jti" -- so the two are recognisable to the
// same tooling. A hand-rolled implementation rather than a JWT library
// because HS256 is an HMAC over two base64url segments, and the
// library-shaped risk here (accepting "alg": "none", or letting the token
// choose its own algorithm) is precisely what an explicit implementation
// avoids: the algorithm below is a constant, never read from the header.

const (
	// TokenType is flask_jwt_extended's discriminator. A refresh token
	// presented as an access token must not be accepted.
	TokenType = "access"
	// DefaultTokenTTL is deliberately longer than the runtime's 15-minute
	// default: a version change involves an image pull that can run for many
	// minutes on a slow device, and having the caller's token expire midway
	// through would strand a device mid-update. The bootloader owns its own
	// sessions, so this does not have to match the runtime's.
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

// bootloaderKey derives the bootloader's signing key from the runtime's
// secret.
//
// HMAC with a fixed domain string: a one-way function of the shared secret
// that the runtime never computes, so neither service can verify the other's
// tokens. Anyone who can read .env can derive it, which is the point -- this
// separates two token spaces on one device, it is not a secret from the host.
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
