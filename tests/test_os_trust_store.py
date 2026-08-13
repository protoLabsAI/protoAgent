"""httpx's default verification never discovers the OS trust store: it checks the
``SSL_CERT_FILE`` env var, then falls back to the bundled ``certifi`` root list —
either way, a private CA an operator installed in the OS trust store is invisible
to it, even though the OS's own HTTP clients trust it fine, which broke A2A
delegate calls to a peer behind an internal CA on the packaged Windows desktop
(#2643).
``truststore.inject_into_ssl()`` fixes that by replacing ``ssl.SSLContext``
(and the urllib3/requests references that hold their own copy of it) process-wide
with one that verifies through the native OS trust APIs instead — these pin that
the injection happens, never blocks boot, an untrusted chain still fails closed
(no ``verify=False`` regression), and — Windows and Linux, CI-gated since both
mutate the real trust store — a CA installed in the OS store is genuinely trusted
end-to-end through a real TLS handshake."""

from __future__ import annotations

import datetime
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager

import httpx
import pytest

truststore = pytest.importorskip("truststore")
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import server

_ORIGINAL_SSL_CONTEXT = ssl.SSLContext  # captured before any test in this file can inject

# Whether it's safe to mutate the REAL system/user certificate trust store: only on a
# CI runner (ephemeral, destroyed after the job) — never on a contributor's own machine,
# even if the platform tool happens to be present there too.
_ON_CI = os.environ.get("CI") == "true"


@pytest.fixture(autouse=True)
def _restore_ssl_context():
    """``inject_into_ssl()`` patches THREE module-level references — ``ssl.SSLContext``,
    ``urllib3.util.ssl_.SSLContext``, and (when present) ``requests.adapters.
    _preloaded_ssl_context`` — process-wide. Restoring only ``ssl.SSLContext`` by hand
    leaves the other two patched, so a later, unrelated test using urllib3/requests
    directly could still run against a truststore-backed context even though
    ``ssl.SSLContext`` itself looks restored. ``truststore.extract_from_ssl()`` is the
    library's own undo for the first two; the third has no library-provided undo, so
    snapshot/restore it by hand (a no-op in environments where it doesn't exist)."""
    try:
        import requests.adapters as _requests_adapters
    except ImportError:
        _requests_adapters = None
    _unset = object()
    preloaded_before = getattr(_requests_adapters, "_preloaded_ssl_context", _unset) if _requests_adapters else _unset
    yield
    truststore.extract_from_ssl()
    if _requests_adapters is not None and preloaded_before is not _unset:
        _requests_adapters._preloaded_ssl_context = preloaded_before


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


# certutil -addstore, Import-Certificate, AND X509Store("Root", "CurrentUser").Add()
# all show an interactive "install this root certificate?" confirmation dialog —
# this is a deliberate Windows security gate on the CurrentUser\Root store
# specifically, since any non-admin user can write there (dotnet/runtime#24160
# confirms X509Store.Add() itself pops it, not just the CLI tools). On a headless
# runner there's nobody to click it, so the process blocks until an external
# timeout kills it. LocalMachine\Root does NOT show the dialog: being able to
# write there at all already requires admin, so Windows doesn't layer an extra
# confirmation on top of that gate — and the CI runner user (GH Actions:
# `runneradmin`) is an administrator. This also better matches how an org
# actually deploys an internal CA in practice (machine-wide via GPO, not
# per-user) than CurrentUser would have.
_ADD_TRUST_PS1 = """
param([string]$CertPath)
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CertPath)
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "LocalMachine")
$store.Open("ReadWrite")
$store.Add($cert)
$store.Close()
"""

_REMOVE_TRUST_PS1 = """
param([string]$Thumbprint)
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "LocalMachine")
$store.Open("ReadWrite")
$found = $store.Certificates.Find([System.Security.Cryptography.X509Certificates.X509FindType]::FindByThumbprint, $Thumbprint, $false)
foreach ($c in $found) { $store.Remove($c) }
$store.Close()
"""


@pytest.mark.skipif(
    not (os.name == "nt" and _ON_CI),
    reason="mutates the real Windows cert store — CI-only (ephemeral runner)",
)
def test_a_ca_trusted_in_the_windows_store_is_trusted_after_injection(tmp_path):
    ca_cert, ca_key = _make_ca()
    leaf_cert, leaf_key = _make_leaf(ca_cert, ca_key)
    cert_path = tmp_path / "leaf.pem"
    key_path = tmp_path / "leaf.key"
    # DER, not PEM: unambiguous for X509Certificate2's file constructor, no format
    # auto-detection involved.
    ca_der_path = tmp_path / "ca.cer"
    cert_path.write_bytes(_pem(leaf_cert))
    key_path.write_bytes(_pem_key(leaf_key))
    ca_der_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.DER))
    thumbprint = ca_cert.fingerprint(hashes.SHA1()).hex().upper()

    add_script = tmp_path / "add_trust.ps1"
    add_script.write_text(_ADD_TRUST_PS1)
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(add_script),
            "-CertPath",
            str(ca_der_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
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
        remove_script = tmp_path / "remove_trust.ps1"
        remove_script.write_text(_REMOVE_TRUST_PS1)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(remove_script),
                "-Thumbprint",
                thumbprint,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


# ── behavior: an OS-trusted private CA is honored (Linux — the leg every PR runs) ──
#
# SSL_CERT_FILE is NOT a stand-in for this: httpx already honors that env var in its
# own default-verify branch, with or without truststore injected — it would prove
# nothing about truststore's actual OS-trust-store path. The real, distinct thing
# truststore's Linux backend (`truststore._openssl`) adds is consulting the system
# default verify paths (`ssl.get_default_verify_paths()`) truststore-side, in a
# process where `ssl.SSLContext` itself is a `truststore.SSLContext` — so the only
# genuine test installs into the actual system trust store via `update-ca-certificates`
# and confirms the SAME request still fails pre-injection (proving this isn't the
# SSL_CERT_FILE shortcut) before it succeeds post-injection.


@pytest.mark.skipif(
    not (_ON_CI and shutil.which("update-ca-certificates") and shutil.which("sudo")),
    reason="mutates the real system CA trust store via sudo update-ca-certificates — CI-only (ephemeral runner), Debian/Ubuntu only",
)
def test_a_ca_trusted_in_the_linux_system_store_is_trusted_after_injection(tmp_path, monkeypatch):
    ca_cert, ca_key = _make_ca()
    leaf_cert, leaf_key = _make_leaf(ca_cert, ca_key)
    cert_path = tmp_path / "leaf.pem"
    key_path = tmp_path / "leaf.key"
    generated_ca_path = tmp_path / "ca.pem"  # writable by this (non-root) test process
    cert_path.write_bytes(_pem(leaf_cert))
    key_path.write_bytes(_pem_key(leaf_key))
    generated_ca_path.write_bytes(_pem(ca_cert))

    # Rule out the unrelated env-var shortcut (SSL_CERT_FILE) and its capath sibling
    # (SSL_CERT_DIR) — both feed ssl.get_default_verify_paths(), which is exactly what
    # this test needs to bypass to exercise the real system-store path. monkeypatch
    # restores whatever was there after the test, unlike a raw os.environ.pop.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    # /usr/local/share/ca-certificates/ is root-owned — the CI runner user isn't root,
    # so installing/removing here goes through sudo (GH-hosted ubuntu-latest runners
    # grant the job user passwordless sudo for exactly this kind of setup step).
    installed_ca_path = "/usr/local/share/ca-certificates/protoagent-test-ca.crt"

    with _serve_tls(str(cert_path), str(key_path)) as port:
        # Reproduces the reported bug (Linux analog): installed in the system store,
        # but plain certifi-only httpx (pre-injection) still fails closed.
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"https://localhost:{port}/", timeout=5)

        subprocess.run(
            ["sudo", "cp", str(generated_ca_path), installed_ca_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            subprocess.run(["sudo", "update-ca-certificates"], check=True, capture_output=True, text=True, timeout=30)

            with pytest.raises(httpx.ConnectError):  # still fails — the CA install alone isn't enough
                httpx.get(f"https://localhost:{port}/", timeout=5)

            server._ensure_os_trust_store()

            resp = httpx.get(f"https://localhost:{port}/", timeout=5)
            assert resp.status_code == 200
        finally:
            subprocess.run(
                ["sudo", "rm", "-f", installed_ca_path], check=False, capture_output=True, text=True, timeout=30
            )
            subprocess.run(["sudo", "update-ca-certificates"], check=False, capture_output=True, text=True, timeout=30)
