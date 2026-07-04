"""Extended notification channels: mobile push, SMTP email, Discord webhook."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from .analyzer import AnalysisResult

_LOGGER = logging.getLogger(__name__)

_HA_API = "http://supervisor/core/api"
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class NotificationDispatcher:
    """Sends suspicious-activity alerts via mobile, email, and Discord."""

    def __init__(
        self,
        supervisor_token: str = "",
        mobile_app_target: str = "",
        mobile_app_enabled: bool = False,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        smtp_recipients: list[str] | None = None,
        smtp_sender: str = "",
        smtp_enabled: bool = False,
        discord_webhook_url: str = "",
        discord_enabled: bool = False,
    ) -> None:
        self._token = supervisor_token
        self._mobile_target = mobile_app_target
        self._mobile_enabled = mobile_app_enabled
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_password = smtp_password
        self._smtp_recipients = smtp_recipients or []
        self._smtp_sender = smtp_sender
        self._smtp_enabled = smtp_enabled
        self._discord_url = discord_webhook_url
        self._discord_enabled = discord_enabled
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        # No default headers here: this session is shared with
        # send_discord(), which posts to an arbitrary user-configured Discord
        # webhook URL. The Supervisor token is attached per-request in
        # send_mobile() instead, so it's only ever sent to the HA API and
        # never leaked to Discord's servers.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ------------------------------------------------------------------
    # Dispatch (entry point)
    # ------------------------------------------------------------------

    async def dispatch(self, result: AnalysisResult, clip: dict[str, Any]) -> None:
        """Send alerts for a suspicious analysis result via all enabled channels."""
        if not result.is_suspicious:
            return

        camera = result.camera or clip.get("camera", "Unknown")
        title = f"Suspicious Activity — {camera}"
        body = (
            f"Camera: {camera}\n"
            f"Confidence: {result.confidence:.0%}\n"
            f"Summary: {result.summary}\n"
            f"Time: {result.analyzed_at}"
        )

        if self._mobile_enabled:
            await self.send_mobile(title, body)
        if self._smtp_enabled:
            await self.send_email(title, body)
        if self._discord_enabled:
            await self.send_discord(title, result.summary, camera, result.confidence)

    # ------------------------------------------------------------------
    # Mobile App (HA Companion)
    # ------------------------------------------------------------------

    async def send_mobile(self, title: str, message: str) -> bool:
        """Send a push notification via HA's mobile_app integration."""
        if not self._mobile_enabled or not self._mobile_target or not self._token:
            return False
        try:
            session = await self._get_session()
            async with session.post(
                f"{_HA_API}/services/notify/{self._mobile_target}",
                json={"title": title, "message": message},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    _LOGGER.info("Mobile notification sent to %s", self._mobile_target)
                    return True
                _LOGGER.warning("Mobile notify returned HTTP %d", resp.status)
                return False
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Mobile notification failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Email (SMTP)
    # ------------------------------------------------------------------

    async def send_email(self, subject: str, body: str) -> bool:
        """Send an email via SMTP."""
        if not self._smtp_enabled or not self._smtp_host or not self._smtp_recipients:
            return False

        try:
            import aiosmtplib

            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = self._smtp_sender or self._smtp_user
            msg["To"] = ", ".join(self._smtp_recipients)
            msg["Date"] = datetime.now(timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S +0000"
            )

            # Port 465 is implicit TLS (the connection is TLS from the first
            # byte); STARTTLS is a different, incompatible negotiation used
            # by port 587/25. Sending start_tls=True to a 465 server hangs
            # or fails the handshake, so branch on the configured port.
            implicit_tls = self._smtp_port == 465
            await aiosmtplib.send(
                msg,
                hostname=self._smtp_host,
                port=self._smtp_port,
                username=self._smtp_user or None,
                password=self._smtp_password or None,
                start_tls=not implicit_tls,
                use_tls=implicit_tls,
            )
            _LOGGER.info(
                "Email sent to %s via %s", self._smtp_recipients, self._smtp_host
            )
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Email notification failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Discord Webhook
    # ------------------------------------------------------------------

    async def send_discord(
        self,
        title: str,
        description: str,
        camera: str = "",
        confidence: float = 0.0,
    ) -> bool:
        """Post an embed to a Discord webhook."""
        if not self._discord_enabled or not self._discord_url:
            return False

        color = 0xFF0000 if confidence > 0.7 else 0xFF8C00
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "fields": [
                        {"name": "Camera", "value": camera, "inline": True},
                        {
                            "name": "Confidence",
                            "value": f"{confidence:.0%}",
                            "inline": True,
                        },
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
        try:
            session = await self._get_session()
            async with session.post(
                self._discord_url,
                json=payload,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status < 400:
                    _LOGGER.info("Discord notification sent")
                    return True
                _LOGGER.warning("Discord webhook returned HTTP %d", resp.status)
                return False
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Discord notification failed: %s", exc)
            return False
