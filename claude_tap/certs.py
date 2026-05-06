"""Local CA + per-host certificate generation for forward-mode TLS termination.

The CA is created once on first use of ``forward`` mode and cached on disk
under :func:`claude_tap.paths.data_dir`. Per-host leaf certs are kept in
process memory.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import ssl
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from claude_tap.paths import data_dir, legacy_data_dir

log = logging.getLogger("claude_tap")

_CA_VALIDITY_DAYS = 5 * 365
_HOST_VALIDITY_DAYS = 365


def _generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def ensure_ca(ca_dir: Path | None = None) -> tuple[Path, Path]:
    """Return ``(cert_path, key_path)``, creating the CA if needed.

    For backwards compatibility, if no CA exists at the new XDG-style path but
    one exists at the legacy ``~/.claude-tap`` location, we reuse it instead
    of generating a fresh one (which would force users to re-trust).
    """

    target = ca_dir or data_dir()
    target.mkdir(parents=True, exist_ok=True)

    cert_path = target / "ca.pem"
    key_path = target / "ca-key.pem"

    if cert_path.exists() and key_path.exists():
        try:
            _load_ca(cert_path, key_path)
            return cert_path, key_path
        except Exception:
            log.warning("existing CA at %s is invalid; regenerating", target)

    legacy = legacy_data_dir()
    legacy_cert = legacy / "ca.pem"
    legacy_key = legacy / "ca-key.pem"
    if ca_dir is None and legacy_cert.exists() and legacy_key.exists():
        try:
            _load_ca(legacy_cert, legacy_key)
            cert_path.write_bytes(legacy_cert.read_bytes())
            key_path.write_bytes(legacy_key.read_bytes())
            key_path.chmod(0o600)
            log.info("migrated CA from %s to %s", legacy, target)
            return cert_path, key_path
        except Exception:
            pass

    log.info("generating new CA in %s", target)
    key = _generate_key()
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "claude-tap CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "claude-tap"),
        ]
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=_CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _load_ca(cert_path: Path, key_path: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    return cert, key  # type: ignore[return-value]


class CertificateAuthority:
    """In-memory CA that mints leaf certs on demand and caches them."""

    def __init__(self, cert_path: Path, key_path: Path) -> None:
        self._cert, self._key = _load_ca(cert_path, key_path)
        self._cache: dict[str, tuple[bytes, bytes]] = {}

    def get_host_pem(self, hostname: str) -> tuple[bytes, bytes]:
        cached = self._cache.get(hostname)
        if cached is not None:
            return cached

        key = _generate_key()
        now = _dt.datetime.now(_dt.timezone.utc)

        san: list[x509.GeneralName] = []
        try:
            ip = ipaddress.ip_address(hostname)
            san.append(x509.IPAddress(ip))
        except ValueError:
            san.append(x509.DNSName(hostname))

        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + _dt.timedelta(days=_HOST_VALIDITY_DAYS))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()),
                critical=False,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._cache[hostname] = (cert_pem, key_pem)
        return cert_pem, key_pem

    def make_ssl_context(self, hostname: str) -> ssl.SSLContext:
        cert_pem, key_pem = self.get_host_pem(hostname)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # ``load_cert_chain`` only takes file paths; persist briefly to disk.
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cf:
            cf.write(cert_pem)
            cert_file = cf.name
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
            kf.write(key_pem)
            key_file = kf.name
        try:
            ctx.load_cert_chain(cert_file, key_file)
        finally:
            Path(cert_file).unlink(missing_ok=True)
            Path(key_file).unlink(missing_ok=True)
        return ctx
