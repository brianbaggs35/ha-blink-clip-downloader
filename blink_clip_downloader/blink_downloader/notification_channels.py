"""Extended notification channels: mobile push, SMTP email, Discord webhook."""

from __future__ import annotations

import html
import json
import logging
from datetime import UTC, datetime
from email.message import EmailMessage
from email.mime.text import MIMEText
from email.utils import make_msgid
from typing import TYPE_CHECKING, Any

import aiohttp

from .rich_alerts import AlertExtras, format_local_time, parse_instant

if TYPE_CHECKING:
    from .analyzer import AnalysisResult
    from .rich_alerts import RichAlertBuilder

_LOGGER = logging.getLogger(__name__)

# Internal HA Supervisor API on the isolated `hassio` Docker network — not
# exposed externally and does not terminate TLS, so http:// is correct here.
_HA_API = "http://supervisor/core/api"  # NOSONAR
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_TEST_NOTIFICATION_TITLE = "Blink Clip Downloader — Test Notification"
# Only the companion app's own notify services understand a picture URL, a
# tap target and buttons. Anything else (a notify group, another platform)
# keeps getting the plain text it always got, since an unknown key can make
# another platform reject the whole message.
_COMPANION_APP_PREFIX = "mobile_app_"
_NOT_A_THREAT_TITLE = "Not a threat"
_KEY_FRAME_FILENAME = "keyframe.jpg"


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
        rich: RichAlertBuilder | None = None,
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
        # Builds each alert's picture, clip link and "Not a threat" button
        # (see rich_alerts.py). None sends the plain text alerts alone.
        self._rich = rich
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
        if self._rich is not None:
            await self._rich.close()

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
        extras = await self._build_extras(result, clip)
        # The deterministic assessment, when there is one. Two independent
        # judgements reached the same alert and an alert that reports only
        # the model's own confidence hides half of why it fired — including,
        # in the override case, the half that fired it.
        assessment = ""
        if result.risk_score > 0:
            assessment = f"Risk: {result.risk_score:.0f}/100 ({result.severity})\n"
            if result.risk_override_applied:
                assessment += "Flagged on detection evidence, not by the AI model.\n"
        body = (
            f"Camera: {camera}\n"
            f"Confidence: {result.confidence:.0%}\n"
            f"{assessment}"
            f"Summary: {result.summary}\n"
            f"{self._time_line(result, extras)}"
        )

        if self._mobile_enabled:
            await self.send_mobile(title, body, extras=extras)
        if self._smtp_enabled:
            await self.send_email(title, body, extras=extras)
        if self._discord_enabled:
            await self.send_discord(
                title,
                result.summary,
                camera,
                result.confidence,
                risk=result.risk_score if result.risk_score > 0 else None,
                severity=result.severity,
                risk_override=result.risk_override_applied,
                extras=extras,
            )
        if self._ha_notify_enabled:
            await self.send_ha_notification(title, body)

    @property
    def _phone_is_companion_app(self) -> bool:
        return bool(
            self._mobile_enabled
            and self._token
            and self._mobile_target.startswith(_COMPANION_APP_PREFIX)
        )

    async def _build_extras(
        self, result: AnalysisResult, clip: dict[str, Any]
    ) -> AlertExtras:
        """This alert's picture, links and button — whatever could be gathered.

        Never raises: the builder already guards each part, and this guards
        the builder, because an alert without its extras still has to go out.
        """
        fallback = AlertExtras(clip_id=str(clip.get("id") or result.clip_id or ""))
        if self._rich is None:
            return fallback
        external = self._smtp_enabled or self._discord_enabled
        try:
            return await self._rich.build(
                result,
                clip,
                attach_image=external,
                phone=self._phone_is_companion_app,
                external_link=external,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Sending a plain alert; its extras failed: %s", exc)
            return fallback

    @staticmethod
    def _time_line(result: AnalysisResult, extras: AlertExtras) -> str:
        """When it happened: the clip's local recorded time when known."""
        if extras.recorded_local:
            return f"Recorded: {extras.recorded_local}"
        analyzed = parse_instant(result.analyzed_at)
        if analyzed is not None:
            return f"Analyzed: {format_local_time(analyzed)}"
        return f"Time: {result.analyzed_at}"

    async def dispatch_battery_alert(
        self, camera: str, battery_state: str, battery_voltage: int | None = None
    ) -> None:
        """Send a low-battery alert via every enabled channel.

        Deliberately reuses the exact same mobile/SMTP/Discord/HA-persistent
        channels (and their existing enabled flags) as :meth:`dispatch`
        above, rather than introducing separate battery-specific channel
        settings — so a Discord webhook already set up for suspicious-clip
        alerts is used for battery alerts too, with no extra configuration.
        Callers (battery_monitor.py) are expected to only call this for a
        genuine ok-to-low transition, gated on the separate
        battery_alerts_enabled master switch. battery_level is deliberately
        never included in the message — Blink's battery_state ("ok"/"low")
        is the one reliably meaningful signal; battery_level is a coarse,
        non-percentage number that would misleadingly read as more precise
        than it is.
        """
        title = f"Low Battery — {camera}"
        body = f"Camera: {camera}\nBattery: {battery_state.strip().capitalize()}"
        if battery_voltage is not None:
            body += f"\nVoltage: {battery_voltage / 100:.2f}V"
        body += f"\nTime: {datetime.now(UTC).isoformat()}"

        if self._mobile_enabled:
            await self.send_mobile(title, body)
        if self._smtp_enabled:
            await self.send_email(title, body)
        if self._discord_enabled:
            await self.send_discord(
                title, f"{camera}'s battery is low.", camera, confidence=None
            )
        if self._ha_notify_enabled:
            await self.send_ha_notification(title, body)

    # ------------------------------------------------------------------
    # Mobile App (HA Companion)
    # ------------------------------------------------------------------

    async def send_mobile(
        self, title: str, message: str, *, extras: AlertExtras | None = None
    ) -> bool:
        """Send a push notification via HA's mobile_app integration.

        With *extras*, a companion-app target also gets the key frame, a tap
        that opens the clip and the "Not a threat" button. If Home Assistant
        refuses that richer message, the plain one is sent instead, so a
        push that worked before this existed still arrives.
        """
        if not self._mobile_enabled or not self._mobile_target or not self._token:
            return False
        data = self._companion_app_data(extras)
        if data:
            status = await self._post_mobile(title, message, data)
            if status in (200, 201):
                return True
            if status is not None:
                _LOGGER.warning(
                    "Mobile notify rejected the alert's picture/actions "
                    "(HTTP %d); resending it as plain text",
                    status,
                )
                return await self._send_mobile_now(title, message)
            return False
        return await self._send_mobile_now(title, message)

    def _companion_app_data(self, extras: AlertExtras | None) -> dict[str, Any]:
        """The companion app's ``data`` for an alert, or {} when there is none.

        ``url`` is the iOS app's tap target and ``clickAction`` Android's;
        both are paths into Home Assistant, which the app opens itself.
        ``authenticationRequired`` makes a locked phone ask to be unlocked
        before "Not a threat" counts, since that button teaches the camera
        to alert less.
        """
        if extras is None or not self._mobile_target.startswith(_COMPANION_APP_PREFIX):
            return {}
        data: dict[str, Any] = {}
        if extras.image_url:
            data["image"] = extras.image_url
        if extras.open_path:
            data["url"] = extras.open_path
            data["clickAction"] = extras.open_path
        if extras.not_a_threat_action:
            data["actions"] = [
                {
                    "action": extras.not_a_threat_action,
                    "title": _NOT_A_THREAT_TITLE,
                    "authenticationRequired": True,
                }
            ]
        if data and extras.clip_id:
            # A second alert about the same clip (Analyze now) replaces the
            # first on the phone rather than stacking beside it.
            data["tag"] = f"blink-clip-{extras.clip_id}"
        return data

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
            _TEST_NOTIFICATION_TITLE,
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
        status = await self._post_mobile(title, message)
        if status in (200, 201):
            return True
        if status is not None:
            _LOGGER.warning("Mobile notify returned HTTP %d", status)
        return False

    async def _post_mobile(
        self, title: str, message: str, data: dict[str, Any] | None = None
    ) -> int | None:
        """POST one push; the HTTP status, or None when the request failed."""
        payload: dict[str, Any] = {"title": title, "message": message}
        if data:
            payload["data"] = data
        try:
            session = self._get_session()
            async with session.post(
                f"{_HA_API}/services/notify/{self._mobile_target}",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    _LOGGER.info("Mobile notification sent to %s", self._mobile_target)
                return resp.status
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Mobile notification failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Email (SMTP)
    # ------------------------------------------------------------------

    async def send_email(
        self, subject: str, body: str, *, extras: AlertExtras | None = None
    ) -> bool:
        """Send an email via SMTP.

        With *extras* carrying a key frame or a clip link, the email gets an
        HTML part showing the picture inline and linking to the clip, beside
        the same plain text a text-only mail client shows.
        """
        if not self._smtp_enabled or not self._smtp_host or not self._smtp_recipients:
            return False
        return await self._send_email_now(subject, body, extras)

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

    async def _send_email_now(
        self, subject: str, body: str, extras: AlertExtras | None = None
    ) -> bool:
        try:
            import aiosmtplib

            msg = self._email_message(body, extras)
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

    @staticmethod
    def _email_message(
        body: str, extras: AlertExtras | None
    ) -> MIMEText | EmailMessage:
        """The message itself, before its headers: plain unless it has extras."""
        image = extras.image if extras is not None else None
        link = extras.open_url if extras is not None else None
        if image is None and not link:
            return MIMEText(body, "plain", "utf-8")
        msg = EmailMessage()
        msg.set_content(f"{body}\nOpen clip: {link}" if link else body)
        lines = "<br>".join(html.escape(line) for line in body.splitlines())
        parts = [f"<p>{lines}</p>"]
        cid = make_msgid(domain="blink-clip-downloader.local")
        if image is not None:
            parts.append(
                f'<p><img src="cid:{cid[1:-1]}" alt="Key frame from the clip" '
                'style="max-width:100%;height:auto"></p>'
            )
        if link:
            parts.append(f'<p><a href="{html.escape(link)}">Open clip</a></p>')
        msg.add_alternative(
            f"<html><body>{''.join(parts)}</body></html>", subtype="html"
        )
        html_part = msg.get_body(preferencelist=("html",))
        if image is not None and html_part is not None:
            html_part.add_related(
                image,
                maintype="image",
                subtype="jpeg",
                cid=cid,
                disposition="inline",
                filename=_KEY_FRAME_FILENAME,
            )
        return msg

    # ------------------------------------------------------------------
    # Discord Webhook
    # ------------------------------------------------------------------

    async def send_discord(
        self,
        title: str,
        description: str,
        camera: str = "",
        confidence: float | None = 0.0,
        risk: float | None = None,
        severity: str = "",
        risk_override: bool = False,
        *,
        extras: AlertExtras | None = None,
    ) -> bool:
        """Post an embed to a Discord webhook.

        *risk*/*severity*/*risk_override* carry the deterministic
        assessment, matching what the mobile/email/HA bodies already say.
        An alert reporting only the model's own confidence hides half of why
        it fired — and in the override case, the half that fired it.

        *extras* adds the key frame as the embed's image (uploaded with the
        message, so Discord never needs to reach the add-on), makes the
        title a link to the clip, and stamps the embed with when the clip
        was recorded rather than when it was analyzed.
        """
        if not self._discord_enabled or not self._discord_url:
            return False
        return await self._send_discord_now(
            title,
            description,
            camera,
            confidence,
            risk,
            severity,
            risk_override,
            extras=extras,
        )

    async def send_test_discord(self) -> tuple[bool, str]:
        """Post a one-off test embed, ignoring discord_enabled.

        Same rationale as :meth:`send_test_email` — verify the webhook URL
        works from the Automations tab before flipping the channel on.
        """
        if not self._discord_url:
            return False, "Discord webhook URL is not configured."
        ok = await self._send_discord_now(
            _TEST_NOTIFICATION_TITLE,
            "This is a test message from the Blink Clip Downloader add-on. "
            "If you received this, your Discord webhook is working correctly.",
            camera="Test",
            confidence=0.0,
        )
        if ok:
            return True, "Test message sent to Discord."
        return False, "Failed to send test message — check the add-on logs for details."

    async def _send_discord_now(
        self,
        title: str,
        description: str,
        camera: str,
        confidence: float | None,
        risk: float | None = None,
        severity: str = "",
        risk_override: bool = False,
        *,
        extras: AlertExtras | None = None,
    ) -> bool:
        # confidence=None (battery alerts — see dispatch_battery_alert) skips
        # the Confidence field entirely rather than showing a meaningless
        # "Confidence: 0%"/"100%" on an event that isn't a probabilistic AI
        # verdict; it still counts as "high urgency" for the embed color.
        color = 0xFF0000 if confidence is None or confidence > 0.7 else 0xFF8C00
        fields = [{"name": "Camera", "value": camera, "inline": True}]
        if confidence is not None:
            fields.append(
                {"name": "Confidence", "value": f"{confidence:.0%}", "inline": True}
            )
        if risk is not None:
            label = f"{risk:.0f}/100"
            if severity:
                label += f" ({severity})"
            fields.append({"name": "Risk", "value": label, "inline": True})
        if risk_override:
            fields.append(
                {
                    "name": "Why",
                    "value": "Flagged on detection evidence, not by the AI model.",
                    "inline": False,
                }
            )
        embed: dict[str, Any] = {
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "timestamp": (extras.recorded_iso if extras else "")
            or datetime.now(UTC).isoformat(),
        }
        if extras is not None and extras.open_url:
            embed["url"] = extras.open_url
        payload = {"embeds": [embed]}
        image = extras.image if extras is not None else None
        if image is None:
            status = await self._post_discord(payload)
        else:
            embed["image"] = {"url": f"attachment://{_KEY_FRAME_FILENAME}"}
            status = await self._post_discord(payload, image)
            if status is not None and status >= 400:
                # Whatever Discord disliked about the upload, the alert
                # itself still has to arrive.
                _LOGGER.warning(
                    "Discord rejected the alert's picture (HTTP %d); "
                    "resending without it",
                    status,
                )
                del embed["image"]
                status = await self._post_discord(payload)
        if status is None:
            return False
        if status < 400:
            _LOGGER.info("Discord notification sent")
            return True
        _LOGGER.warning("Discord webhook returned HTTP %d", status)
        return False

    async def _post_discord(
        self, payload: dict[str, Any], image: bytes | None = None
    ) -> int | None:
        """POST one webhook message; the HTTP status, or None if it failed.

        With *image*, the message goes as multipart with the picture as its
        one file, which the embed refers to as ``attachment://``.
        """
        body: dict[str, Any]
        if image is None:
            body = {"json": payload}
        else:
            form = aiohttp.FormData()
            form.add_field(
                "payload_json", json.dumps(payload), content_type="application/json"
            )
            form.add_field(
                "files[0]",
                image,
                filename=_KEY_FRAME_FILENAME,
                content_type="image/jpeg",
            )
            body = {"data": form}
        try:
            session = self._get_session()
            async with session.post(
                self._discord_url, timeout=_TIMEOUT, **body
            ) as resp:
                return resp.status
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Discord notification failed: %s", exc)
            return None

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
            _TEST_NOTIFICATION_TITLE,
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
