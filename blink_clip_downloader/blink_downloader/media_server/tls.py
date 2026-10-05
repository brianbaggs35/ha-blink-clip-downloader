"""HTTPS for the direct port: the certificate Home Assistant keeps in /ssl.

The port the web UI listens on also carries Home Assistant's ingress, and
Supervisor reaches that over plain HTTP, so it has to stay plain. With
``ssl`` on, :class:`~blink_downloader.media_server.MediaServer` serves the
same app a second time on :data:`HTTPS_PORT` with the context built here, and
sends anyone who has not signed in there instead of letting a password
cross the network in clear.

No certificate is made here. ``/ssl`` is where Home Assistant's own
certificate lives (the Let's Encrypt and DuckDNS add-ons write
``fullchain.pem`` and ``privkey.pem`` into it), so the defaults reuse it; a
pair of your own goes in the same folder (see DOCS.md).

There is deliberately no ``Strict-Transport-Security`` header. HSTS is
remembered per *host*, not per port, so it would make a browser rewrite
``http://homeassistant.local:8123`` to ``https://`` as well.
"""

from __future__ import annotations

import ssl
from pathlib import Path

#: Where Home Assistant mounts its certificates (``map: ssl`` in config.yaml).
SSL_DIR = Path("/ssl")

#: The container port the TLS listener uses; config.yaml publishes it.
HTTPS_PORT = 8100


class TlsConfigError(Exception):
    """The configured certificate cannot be used; the message says why."""


def _inside_ssl_dir(name: str, option: str) -> Path:
    """*name* as a file under /ssl, or TlsConfigError.

    The options are file names relative to /ssl, as in every other add-on
    that has them. Anything that resolves outside it (``../data/...``, an
    absolute path) is refused rather than read.
    """
    if not name.strip():
        raise TlsConfigError(f"{option} is empty")
    root = SSL_DIR.resolve()
    path = (root / name.strip()).resolve()
    if not path.is_relative_to(root):
        raise TlsConfigError(f"{option} must be a file inside {SSL_DIR}, not {name!r}")
    return path


def load_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """The server-side TLS context for *certfile* and *keyfile* in /ssl.

    Raises :class:`TlsConfigError` for a missing file, a pair that does not
    belong together, or anything else the ``ssl`` module refuses — the
    caller decides what to do about it, and the usual answer is to carry on
    without HTTPS.
    """
    cert = _inside_ssl_dir(certfile, "certfile")
    key = _inside_ssl_dir(keyfile, "keyfile")
    for path in (cert, key):
        if not path.is_file():
            raise TlsConfigError(f"{path} does not exist")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert, key)
    except OSError as exc:  # ssl.SSLError is one
        raise TlsConfigError(
            f"{cert.name} and {key.name} could not be loaded as a certificate "
            f"and its private key: {exc}"
        ) from exc
    return context
