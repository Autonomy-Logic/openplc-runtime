# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Throwaway test PKI for the spike broker. Keys are generated per run, never committed."""

from __future__ import annotations

import datetime as dt
import secrets
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOW = dt.datetime.now(dt.timezone.utc)


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = _key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf(
    ca_key: ec.EllipticCurvePrivateKey,
    ca_cert: x509.Certificate,
    cn: str,
    dns: str | None,
    server: bool,
    expired: bool = False,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = _key()
    before = NOW - dt.timedelta(days=10) if expired else NOW - dt.timedelta(days=1)
    after = NOW - dt.timedelta(days=1) if expired else NOW + dt.timedelta(days=30)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(before)
        .not_valid_after(after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
    )
    if dns is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False
        )
    return key, builder.sign(ca_key, hashes.SHA256())


def _write(
    directory: Path, stem: str, key: ec.EllipticCurvePrivateKey | None, cert: x509.Certificate
) -> None:
    (directory / f"{stem}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if key is not None:
        (directory / f"{stem}.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


def main(out: Path, broker_host: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ca_key, ca = _ca("Spike Broker CA")
    rogue_key, rogue = _ca("Spike Untrusted CA")
    client_ca_key, client_ca = _ca("Spike Client CA")
    _write(out, "ca", None, ca)
    _write(out, "client-ca", None, client_ca)
    _write(out, "server-ok", *_leaf(ca_key, ca, broker_host, broker_host, True))
    _write(out, "server-wrong-host", *_leaf(ca_key, ca, "wrong.example", "wrong.example", True))
    _write(out, "server-expired", *_leaf(ca_key, ca, broker_host, broker_host, True, expired=True))
    _write(out, "server-untrusted", *_leaf(rogue_key, rogue, broker_host, broker_host, True))
    _write(out, "client-ok", *_leaf(client_ca_key, client_ca, "plc-spike", None, False))
    _write(out, "client-rogue", *_leaf(rogue_key, rogue, "plc-rogue", None, False))
    other_key = _key()
    (out / "client-mismatch.key").write_bytes(
        other_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (out / "password").write_text(f"MARKER-PW-{secrets.token_hex(8)}")
    for path in out.iterdir():
        path.chmod(0o644)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
