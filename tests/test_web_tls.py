import io
import os
from pathlib import Path
from contextlib import redirect_stdout
import ssl
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from athena.web import TLS_CERT_ENV, TLS_KEY_ENV, main, tls_context


def write_pair(directory: str) -> tuple[str, str]:
    # Generate fresh test-only credentials; no private key belongs in Git.
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256()))
    cert = Path(directory) / "dashboard.crt"
    key = Path(directory) / "dashboard.key"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(private_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(cert), str(key)


def run_main(environment: dict[str, str]) -> tuple[int, object, str]:
    output = io.StringIO()
    with (
        patch.dict(os.environ, environment, clear=False),
        # create_app is a coroutine function, so patch would hand back an
        # AsyncMock and leave an un-awaited coroutine behind; a plain mock keeps
        # the test about start-up decisions only.
        patch("athena.web.create_app", MagicMock(return_value=object())),
        patch("athena.web.web.run_app") as run_app,
        patch.object(sys, "argv", ["athena-web"]),
        redirect_stdout(output),
    ):
        code = main()
    return code, run_app, output.getvalue()


class DashboardTlsTests(unittest.TestCase):
    def test_no_paths_leaves_the_dashboard_on_plain_http(self):
        self.assertIsNone(tls_context())

    def test_half_a_key_pair_is_refused_rather_than_served_insecurely(self):
        for cert, key in (("dashboard.crt", ""), ("", "dashboard.key")):
            with self.subTest(cert=cert, key=key):
                with self.assertRaises(ValueError):
                    tls_context(cert, key)

    def test_a_configured_pair_builds_a_modern_context(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = write_pair(directory)
            context = tls_context(cert, key)
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_a_missing_file_raises_instead_of_falling_back_to_http(self):
        with tempfile.TemporaryDirectory() as directory:
            _, key = write_pair(directory)
            with self.assertRaises(OSError):
                tls_context(str(Path(directory) / "absent.crt"), key)

    def test_a_file_that_is_not_a_key_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, _ = write_pair(directory)
            with self.assertRaises(ssl.SSLError):
                tls_context(cert, cert)

    def test_an_unusable_certificate_stops_the_dashboard_and_says_so(self):
        code, run_app, output = run_main({TLS_CERT_ENV: "C:/nope.crt",
                                          TLS_KEY_ENV: "C:/nope.key"})
        self.assertEqual(code, 1)
        run_app.assert_not_called()
        self.assertIn("TLS", output)

    def test_configured_certificate_reaches_the_server(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = write_pair(directory)
            code, run_app, _ = run_main({TLS_CERT_ENV: cert, TLS_KEY_ENV: key})
        self.assertEqual(code, 0)
        self.assertIsInstance(run_app.call_args.kwargs["ssl_context"], ssl.SSLContext)

    def test_without_a_certificate_the_server_still_starts(self):
        code, run_app, _ = run_main({TLS_CERT_ENV: "", TLS_KEY_ENV: ""})
        self.assertEqual(code, 0)
        self.assertIsNone(run_app.call_args.kwargs["ssl_context"])


if __name__ == "__main__":
    unittest.main()
