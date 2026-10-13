# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Throwaway PKI for credential and TLS tests. Generated per session, never committed."""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, rsa
from cryptography.hazmat.primitives.asymmetric.types import (
    CertificateIssuerPrivateKeyTypes,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOW = dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True)
class Pair:
    cert_pem: bytes
    key_pem: bytes


def key_pem(key: CertificateIssuerPrivateKeyTypes, passphrase: bytes | None = None) -> bytes:
    encryption: serialization.KeySerializationEncryption = (
        serialization.NoEncryption()
        if passphrase is None
        else serialization.BestAvailableEncryption(passphrase)
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
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


def leaf(
    issuer: tuple[ec.EllipticCurvePrivateKey, x509.Certificate],
    cn: str,
    server: bool,
    host: str | None = None,
    before: dt.timedelta = dt.timedelta(days=-1),
    after: dt.timedelta = dt.timedelta(days=30),
    key: CertificateIssuerPrivateKeyTypes | None = None,
) -> tuple[CertificateIssuerPrivateKeyTypes, x509.Certificate]:
    own = key if key is not None else ec.generate_private_key(ec.SECP256R1())
    usage = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(issuer[1].subject)
        .public_key(own.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW + before)
        .not_valid_after(NOW + after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
    )
    if host is not None:
        names: list[x509.GeneralName] = [x509.DNSName(host)]
        if host == "localhost":
            names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    return own, builder.sign(issuer[0], hashes.SHA256())


def pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def pair(item: tuple[CertificateIssuerPrivateKeyTypes, x509.Certificate]) -> Pair:
    return Pair(pem(item[1]), key_pem(item[0]))


class Pki:
    """Broker CA, client CA, rogue CA and the leaves the tests need."""

    def __init__(self) -> None:
        self.broker_ca = _ca("Test Broker CA")
        self.client_ca = _ca("Test Client CA")
        self.rogue_ca = _ca("Test Rogue CA")
        self.server_ok = pair(leaf(self.broker_ca, "localhost", True, "localhost"))
        self.server_wrong_host = pair(leaf(self.broker_ca, "wrong", True, "wrong.example"))
        self.server_expired = pair(
            leaf(
                self.broker_ca,
                "localhost",
                True,
                "localhost",
                before=dt.timedelta(days=-10),
                after=dt.timedelta(days=-1),
            )
        )
        self.server_untrusted = pair(leaf(self.rogue_ca, "localhost", True, "localhost"))
        self.client_ok = pair(leaf(self.client_ca, "plc-linha1", False))
        self.client_rogue = pair(leaf(self.rogue_ca, "plc-rogue", False))
        self.client_expired = pair(
            leaf(
                self.client_ca,
                "plc-old",
                False,
                before=dt.timedelta(days=-10),
                after=dt.timedelta(days=-1),
            )
        )
        self.client_future = pair(
            leaf(
                self.client_ca,
                "plc-future",
                False,
                before=dt.timedelta(days=1),
                after=dt.timedelta(days=30),
            )
        )
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.client_rsa = pair(leaf(self.client_ca, "plc-rsa", False, key=rsa_key))
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        self.client_weak_rsa = pair(leaf(self.client_ca, "plc-weak", False, key=weak))
        self.other_key_pem = key_pem(ec.generate_private_key(ec.SECP256R1()))
        self.dsa_key_pem = key_pem(dsa.generate_private_key(2048))

    @property
    def broker_ca_pem(self) -> bytes:
        return pem(self.broker_ca[1])

    @property
    def client_ca_pem(self) -> bytes:
        return pem(self.client_ca[1])

    def write(self, directory: Path, name: str, data: bytes) -> str:
        path = directory / name
        path.write_bytes(data)
        return str(path)
