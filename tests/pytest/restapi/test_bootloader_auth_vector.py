"""Python half of the bootloader's shared authentication vector (RTOP-283).

The bootloader is written in Go and reimplements two formats this codebase owns:
Werkzeug's PBKDF2 password hash and Flask-JWT-Extended's HS256 access token.
It has to, because it authenticates callers while the runtime is DOWN -- cold
recovery after a reboot is exactly when there is no runtime to ask.

That makes it the same class of hazard as the ctypes mirror in
``shared/plugin_runtime_args.py``: two implementations of one format, in two
languages, that can drift apart silently. The failure mode is nasty -- every
login on the device stops working, on a device nobody can log in to in order
to find out why.

So both sides pin the identical constants. The Go half asserts them in
``bootloader/internal/runtimeauth/runtimeauth_test.go``; this half asserts that
the libraries here still produce and accept them. If a Werkzeug or PyJWT
upgrade changes either format, the test on the side that changed fails, and
the fix is to regenerate the vector in BOTH files together -- never in one.
"""

import jwt as pyjwt
from werkzeug.security import check_password_hash

# --- the vector -----------------------------------------------------------
# Keep byte-identical with the consts at the top of runtimeauth_test.go.

PEPPER = "a" * 64
PASSWORD = "correct horse battery staple"
STORED_HASH = (
    "pbkdf2:sha256:600000$WCXqtZujfdFXqzAB$"
    "4be2d44037a7d62f2483d1a189bd2dacb66871b323871f185a73a8e2d3230611"
)

JWT_SECRET = "b" * 64
# Minted by create_access_token(identity="7").
VECTOR_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJmcmVzaCI6ZmFsc2UsImlhdCI6MTc4ODQ2NTIwMCwianRpIjoiNWYwNjVlOTctM2M2NC00"
    "ZGU0LWFlYmYtMDMyZjdjNDM5M2M3IiwidHlwZSI6ImFjY2VzcyIsInN1YiI6IjciLCJuYmYi"
    "OjE3ODg0NjUyMDAsImNzcmYiOiJjMThiMDFiMy05NmVjLTQwZDQtYjJmZS1jNjVhYTNiZjUx"
    "NTciLCJleHAiOjE3ODg0NjYxMDB9."
    "xM9AVtdXVFd5mU6gWm842aHwVQpYybL4A3EMyEWSLjc"
)


# --- password hash --------------------------------------------------------


def test_werkzeug_still_accepts_the_vector_hash():
    # The Go side derives this same key with crypto/pbkdf2. If Werkzeug changes
    # its default parameters or its serialisation, this fails here first.
    assert check_password_hash(STORED_HASH, PASSWORD + PEPPER)


def test_the_pepper_is_appended_not_prepended():
    # User.set_password does ``password = password + PEPPER``. The Go side has
    # to match exactly; reversing the order fails every login while looking
    # entirely reasonable in review.
    assert not check_password_hash(STORED_HASH, PEPPER + PASSWORD)


def test_the_hash_advertises_the_parameters_the_bootloader_parses():
    # The Go side reads the iteration count out of the hash rather than
    # assuming 600000, but it only understands pbkdf2/sha256.
    method, _salt, _digest = STORED_HASH.split("$", 2)
    assert method == "pbkdf2:sha256:600000", method


# --- access token ---------------------------------------------------------


def test_pyjwt_verifies_the_vector_token_signature():
    # Signature only: the vector's exp is a fixed timestamp, so asserting
    # freshness would make this pass or fail depending on the clock.
    decoded = pyjwt.decode(
        VECTOR_TOKEN,
        JWT_SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_nbf": False},
    )
    assert decoded["sub"] == "7"
    assert decoded["type"] == "access"


def test_the_identity_claim_is_the_user_id_as_a_string():
    # user_identity_lookup returns str(user.id). The bootloader mints tokens with
    # the same shape so the runtime can consume one it did not issue.
    decoded = pyjwt.decode(
        VECTOR_TOKEN,
        JWT_SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_nbf": False},
    )
    assert isinstance(decoded["sub"], str)


def test_a_token_signed_with_another_secret_is_rejected():
    # Pins the property the whole scheme rests on: both sides share
    # JWT_SECRET_KEY, and nothing else can mint an acceptable token.
    try:
        pyjwt.decode(
            VECTOR_TOKEN,
            "c" * 64,
            algorithms=["HS256"],
            options={"verify_exp": False, "verify_nbf": False},
        )
    except pyjwt.InvalidSignatureError:
        return
    raise AssertionError("a token signed with a different secret must not verify")
