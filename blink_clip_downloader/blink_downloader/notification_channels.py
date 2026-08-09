"""Extended notification channels: mobile push, SMTP email, Discord webhook."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from .analyzer import AnalysisResult

_LOGGER = logging.getLogger(__name__)

# Internal HA Supervisor API on the isolated `hassio` Docker network — not
# exposed externally and does not terminate TLS, so http:// is correct here.
_HA_API = "http://supervisor/core/api"  # NOSONAR
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class NotificationDispatcher:
    """Sends suspicious-activity alerts via mobile, email, and Discord."""

    # Empty-string defaults below mean "channel not configured" — they are
    # placeholders, not credentials (B107).
    def __init__(  # nosec B107
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
        ha_notify_enabled: bool = False,
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
        self._ha_notify_enabled = ha_notify_enabled
        self._session: aiohttp.ClientSession | None = None

    @property
    def smtp_configured(self) -> bool:
        """True if enough SMTP settings are present to attempt sending."""
        return bool(self._smtp_host and self._smtp_recipients)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
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
        if self._ha_notify_enabled:
            await self.send_ha_notification(title, body)

    # ------------------------------------------------------------------
    # Mobile App (HA Companion)
    # ------------------------------------------------------------------

    async def send_mobile(self, title: str, message: str) -> bool:
        """Send a push notification via HA's mobile_app integration."""
        if not self._mobile_enabled or not self._mobile_target or not self._token:
            return False
        return await self._send_mobile_now(title, message)

    async def send_test_mobile(self) -> tuple[bool, str]:
        """Send a one-off test push notification, ignoring mobile_app_enabled.

        Same rationale as :meth:`send_test_email` — verify the target/token
        work from the Automations tab before flipping the channel on.
        """
        if not self._mobile_target:
            return False, "Mobile app target is not configured."
        if not self._token:
            return False, "No Supervisor token available."
        ok = await self._send_mobile_now(
            "Blink Clip Downloader — Test Notification",
            "This is a test push notification from the Blink Clip Downloader "
            "add-on. If you received this, your mobile_app target is working "
            "correctly.",
        )
        if ok:
            return True, f"Test notification sent to {self._mobile_target}."
        return (
            False,
            "Failed to send test notification — check the add-on logs for details.",
        )

    async def _send_mobile_now(self, title: str, message: str) -> bool:
        try:
            session = self._get_session()
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
        return await self._send_email_now(subject, body)

    async def send_test_email(self) -> tuple[bool, str]:
        """Send a one-off test email, ignoring smtp_enabled.

        Lets a user verify SMTP host/credentials from the web UI before
        flipping smtp_enabled on, since real alerts only fire when a clip
        is actually flagged suspicious.
        """
        if not self._smtp_host:
            return False, "SMTP host is not configured."
        if not self._smtp_recipients:
            return False, "No SMTP recipients configured."
        ok = await self._send_email_now(
            "Blink Clip Downloader — Test Email",
            "This is a test email from the Blink Clip Downloader add-on. "
            "If you received this, your SMTP settings are working correctly.",
        )
        if ok:
            return True, f"Test email sent to {', '.join(self._smtp_recipients)}."
        return False, "Failed to send test email — check the add-on logs for details."

    async def _send_email_now(self, subject: str, body: str) -> bool:
        try:
            import aiosmtplib

            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = self._smtp_sender or self._smtp_user
            msg["To"] = ", ".join(self._smtp_recipients)
            msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")

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
        return await self._send_discord_now(title, description, camera, confidence)

    async def send_test_discord(self) -> tuple[bool, str]:
        """Post a one-off test embed, ignoring discord_enabled.

        Same rationale as :meth:`send_test_email` — verify the webhook URL
        works from the Automations tab before flipping the channel on.
        """
        if not self._discord_url:
            return False, "Discord webhook URL is not configured."
        ok = await self._send_discord_now(
            "Blink Clip Downloader — Test Notification",
            "This is a test message from the Blink Clip Downloader add-on. "
            "If you received this, your Discord webhook is working correctly.",
            camera="Test",
            confidence=0.0,
        )
        if ok:
            return True, "Test message sent to Discord."
        return False, "Failed to send test message — check the add-on logs for details."

    async def _send_discord_now(
        self, title: str, description: str, camera: str, confidence: float
    ) -> bool:
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
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            ]
        }
        try:
            session = self._get_session()
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

    # ------------------------------------------------------------------
    # Home Assistant persistent notification (suspicious activity only —
    # distinct from HANotifier/notify_ha, which covers new-clip-downloaded,
    # the daily digest, and system events like 2FA/auth/storage. A user who
    # wants an in-HA alert specifically for suspicious clips, without the
    # per-download noise notify_ha also produces, enables this instead.)
    # ------------------------------------------------------------------

    async def send_ha_notification(self, title: str, message: str) -> bool:
        """Create a persistent notification in Home Assistant."""
        if not self._ha_notify_enabled or not self._token:
            return False
        return await self._send_ha_notification_now(title, message)

    async def send_test_ha_notification(self) -> tuple[bool, str]:
        """Send a one-off test persistent notification, ignoring ha_notify_enabled.

        Same rationale as :meth:`send_test_email` — verify this works from
        the Automations tab before flipping the channel on.
        """
        if not self._token:
            return False, "No Supervisor token available."
        ok = await self._send_ha_notification_now(
            "Blink Clip Downloader — Test Notification",
            "This is a test notification from the Blink Clip Downloader "
            "add-on. If you received this, Home Assistant suspicious-activity "
            "notifications are working correctly.",
        )
        if ok:
            return True, "Test notification sent to Home Assistant."
        return (
            False,
            "Failed to send test notification — check the add-on logs for details.",
        )

    async def _send_ha_notification_now(self, title: str, message: str) -> bool:
        try:
            session = self._get_session()
            async with session.post(
                f"{_HA_API}/services/persistent_notification/create",
                json={"title": title, "message": message},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    _LOGGER.info("HA persistent notification sent")
                    return True
                _LOGGER.warning("HA notify returned HTTP %d", resp.status)
                return False
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("HA persistent notification failed: %s", exc)
            return False
