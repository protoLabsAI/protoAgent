"""httpx (and everything else on the stdlib ``ssl`` module) only ever verifies
against the bundled ``certifi`` root list — a private CA an operator installed in
the OS trust store is invisible to it even though the OS's own HTTP clients trust
it fine, which broke A2A delegate calls to a peer behind an internal CA on the
packaged Windows desktop (#2643). ``truststore.inject_into_ssl()`` fixes that by
replacing ``ssl.SSLContext`` process-wide with one that verifies through the
native OS trust APIs instead — these pin that the injection happens, never blocks
boot, an untrusted chain still fails closed (no ``verify=False`` regression), and
— Windows only, where the bug was reported — a CA installed in the OS store is
genuinely trusted end-to-end through a real TLS handshake."""

from __future__ import annotations

import datetime
import os
import socket
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager

import httpx
import pytest
import truststore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import server

_ORIGINAL_SSL_CONTEXT = ssl.SSLContext  # captured before any test in this file can inject


@pytest.fixture(autouse=True)
def _restore_ssl_context():
    """``inject_into_ssl()`` mutates ``ssl.SSLContext`` process-wide — undo it after
    every test (pass or fail) so later, unrelated tests never see a patched ssl
    module."""
    yield
    ssl.SSLContext = _ORIGINAL_SSL_CONTEXT


# ── cert-chain helpers ──────────────────────────────────────────────────────────


def _make_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "protoAgent test CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _make_leaf(ca_cert, ca_key, hostname="localhost"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def _pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _pem_key(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


@contextmanager
def _serve_tls(cert_path: str, key_path: str):
    """Bind a loopback HTTPS listener presenting the given cert/key and answer
    every request with a bare 200 — just enough for a client to tell "the TLS
    handshake succeeded" apart from "the server sent something odd". Always built
    on the ORIGINAL ssl.SSLContext, regardless of the current test's injection
    state — a truststore-patched context does OS peer-verification unconditionally
    in ``wrap_socket``, which is undefined for server-side use."""
    ctx = _ORIGINAL_SSL_CONTEXT(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(0.2)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def _accept_loop():
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls_conn:
                    tls_conn.recv(4096)
                    tls_conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            except (ssl.SSLError, OSError):
                pass  # the client rejected the handshake — exactly what some tests exercise

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=2)
        sock.close()


# ── wiring ───────────────────────────────────────────────────────────────────────


def test_ensure_os_trust_store_injects(monkeypatch):
    calls = []
    fake = type("_FakeTruststore", (), {"inject_into_ssl": staticmethod(lambda: calls.append(1))})
    monkeypatch.setitem(sys.modules, "truststore", fake)
    server._ensure_os_trust_store()
    assert calls == [1]


def test_missing_truststore_does_not_block_boot(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)  # `import truststore` raises ImportError
    server._ensure_os_trust_store()  # must not raise


def test_injection_makes_ssl_context_os_backed():
    server._ensure_os_trust_store()
    assert ssl.SSLContext is truststore.SSLContext


# ── behavior: fail-closed is preserved ────────────────────────────────────────────


def test_untrusted_self_signed_cert_still_fails_closed(tmp_path):
    """No ``verify=False`` regression: a chain the OS doesn't trust either must
    still fail, even after injection."""
    ca_cert, ca_key = _make_ca()
    leaf_cert, leaf_key = _make_leaf(ca_cert, ca_key)
    cert_path = tmp_path / "leaf.pem"
    key_path = tmp_path / "leaf.key"
    cert_path.write_bytes(_pem(leaf_cert))
    key_path.write_bytes(_pem_key(leaf_key))

    server._ensure_os_trust_store()  # the CA above was never installed anywhere

    with _serve_tls(str(cert_path), str(key_path)) as port:
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"https://localhost:{port}/", timeout=5)


# ── behavior: an OS-trusted private CA is honored (Windows — where #2643 was filed) ──


@pytest.mark.skipif(os.name != "nt", reason="exercises the real Windows cert store via certutil")
def test_a_ca_trusted_in_the_windows_store_is_trusted_after_injection(tmp_path):
    ca_cert, ca_key = _make_ca()
    leaf_cert, leaf_key = _make_leaf(ca_cert, ca_key)
    cert_path = tmp_path / "leaf.pem"
    key_path = tmp_path / "leaf.key"
    ca_path = tmp_path / "ca.pem"
    cert_path.write_bytes(_pem(leaf_cert))
    key_path.write_bytes(_pem_key(leaf_key))
    ca_path.write_bytes(_pem(ca_cert))
    thumbprint = ca_cert.fingerprint(hashes.SHA1()).hex()

    subprocess.run(
        ["certutil", "-user", "-addstore", "Root", str(ca_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        with _serve_tls(str(cert_path), str(key_path)) as port:
            # Reproduces the reported bug: installed in the OS store, but plain
            # certifi-only httpx (pre-injection) still fails closed.
            with pytest.raises(httpx.ConnectError):
                httpx.get(f"https://localhost:{port}/", timeout=5)

            server._ensure_os_trust_store()

            resp = httpx.get(f"https://localhost:{port}/", timeout=5)
            assert resp.status_code == 200
    finally:
        subprocess.run(
            ["certutil", "-user", "-delstore", "Root", thumbprint],
            check=False,
            capture_output=True,
            text=True,
        )
