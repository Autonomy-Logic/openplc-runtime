import datetime
import ipaddress
import os

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# logger = logging.getLogger("logger")


class CertGen:
    """Generates a self-signed TLS certificate and private key."""

    def __init__(self, hostname, ip_addresses=None):
        self.hostname = hostname
        self.ip_addresses = ip_addresses

        self.now = datetime.datetime.utcnow()
        self.subject = self.issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            ]
        )

        self.alt_names = [x509.DNSName(hostname)]
        if ip_addresses:
            for addr in ip_addresses:
                self.alt_names.append(x509.IPAddress(ipaddress.ip_address(addr)))

        self.san_extension = x509.SubjectAlternativeName(self.alt_names)

    def generate_key(self):
        # Generate our key
        self.key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )

    def generate_self_signed_cert(self, cert_file, key_file):
        # logger.debug("Generating self-signed certificate for %s...", self.hostname)

        self.generate_key()

        cert = (
            x509.CertificateBuilder()
            .subject_name(self.subject)
            .issuer_name(self.issuer)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(self.now)
            .not_valid_after(
                self.now + datetime.timedelta(days=365)
            )  # Valid for 1 year
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .add_extension(self.san_extension, critical=False)
            .sign(self.key, hashes.SHA256(), default_backend())
        )

        # Write our certificate and key to disk
        with open(cert_file, "wb+") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_file, "wb+") as f:
            f.write(
                self.key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        # logger.debug("Certificate saved to %s", cert_file)
        # logger.debug("Private key saved to %s", key_file)

    def is_certificate_valid(self, cert_file):
        """Check if the certificate is valid."""
        if not os.path.exists(cert_file):
            # logger.warning("Certificate file not found: %s", cert_file)
            return False

        try:
            with open(cert_file, "rb") as f:
                cert_data = f.read()
            cert = x509.load_pem_x509_certificate(cert_data, default_backend())

            # Create a UTC-aware datetime object
            now = datetime.datetime.now(datetime.timezone.utc)

            if now < cert.not_valid_before_utc:
                # logger.warning("Certificate is not yet valid. Valid from: %s", cert.not_valid_before_utc)
                return False
            if now > cert.not_valid_after_utc:
                # logger.warning("Certificate has expired. Expired on: %s", cert.not_valid_after_utc)
                return False

            # logger.info("Certificate is valid. Expires on: %s", cert.not_valid_after_utc)
            return True

        except Exception as e:
            # logger.error("Error loading or parsing certificate: %s", e)
            return False
