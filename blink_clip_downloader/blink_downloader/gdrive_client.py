"""Google Drive backup client — OAuth device flow + resumable upload/list/delete.

Thin ``aiohttp``-based client, no ``google-api-python-client``/``google-auth``
dependency — this add-on is deliberately dependency-lean (see the base image
size discussion around the optional CV pipeline) and the REST surface needed
here (device auth, resumable upload, list/create folder, trash-delete, quota)
is small enough to implement directly. Mirrors the lazy-session/``close()``
shape every other HTTP-calling module in this package uses (see
``analyzer.py``'s ``ClipAnalyzer``).

Because this add-on runs behind Home Assistant ingress (no fixed,
pre-registerable redirect URL), the connection flow is Google's OAuth 2.0
**Device Flow** ("TVs and Limited Input devices") rather than a redirect-based
sign-in: the user is shown a short code and a URL, completes sign-in on any
device, and this client polls for the resulting token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiofiles
import aiohttp

_LOGGER = logging.getLogger(__name__)

_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
# The OAuth endpoint URL trips bandit's hardcoded-password check purely
# because the constant's name contains "TOKEN" — it is a URL, not a secret.
_TOKEN_URL = "https://oauth2.googleapis.com/token"  # nosec B105
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_DRIVE_API = "https://www.googleapis.com/drive/v3"
_DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

# drive.file only: per-file access to files/folders this app itself creates
# or that the user opens through the app. A broader scope like
# drive.readonly was tried once (to let the folder picker list *all* of the
# user's existing Drive folders, not just ones the app created) and had to
# be reverted — Google's device-flow/limited-input-device OAuth consent
# screen (what this add-on uses, being a TV-like non-browser client) blocks
# sensitive/restricted scopes such as drive.readonly outright, so it broke
# the ability to connect at all. Do not add any scope back without
# confirming it's allowed on that consent flow first, not just doable in a
# browser-based OAuth flow. Everything in this module (browsing folders,
# creating date/camera subfolders, uploads) works within drive.file alone
# because it only ever touches folders the app itself created or that were
# explicitly selected via this app's own folder browser.
_SCOPE = "https://www.googleapis.com/auth/drive.file"

_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# Credentials persisted the same way as Blink's own OAuth token
# (downloader.py's AUTH_FILE/_persist_auth) — atomic tmp+rename, not the
# plain-write convention used for user-editable settings files like
# camera_configs.json. Losing this file mid-write bricks the connection the
# same way losing AUTH_FILE would, so it gets the same care.
CREDENTIALS_FILE = Path("/data/google_drive_credentials.json")

# The OAuth client id/secret (a one-time Google Cloud registration the admin
# does themselves) and the backup policy — both user-editable from the
# Storage tab, so this uses the same plain-write convention as
# vehicle_settings.json/camera_configs.json (Convention B), not the atomic
# convention above: this is an ordinary edited-via-a-form settings file, not
# an opaque bearer token that bricks the connection if a write is lost.
SETTINGS_FILE = Path("/data/google_drive_settings.json")

_BACKUP_POLICIES = {"archived_only", "all_clips"}

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Clip uploads can be large and links slow — a much longer budget than the
# small metadata calls above.
_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=1800)
_QUOTA_CACHE_SECONDS = 60

_JSON_ERRORS = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
    json.JSONDecodeError,
)
_HTTP_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)

PollStatus = Literal["pending", "slow_down", "success", "expired", "denied", "error"]


@dataclass
class DeviceFlowInfo:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass
class TokenPollResult:
    status: PollStatus
    message: str = ""


@dataclass
class DriveQuota:
    limit: int | None  # None means unlimited (Google Workspace accounts)
    usage: int
    usage_in_drive: int


@dataclass
class DriveFolder:
    id: str
    name: str
    modified_time: str = ""


class GDriveClient:
    """Google Drive backup client (device-flow OAuth + resumable upload).

    ``connected``/``rate_limited``/``quota_exceeded`` are plain instance
    attributes rather than computed properties — kept simple since nothing
    here needs property indirection, and so a ``MagicMock(spec=GDriveClient)``
    in tests can have them set directly, exactly like every other provider
    mock in this codebase (see ``test_analysis_queue.py``'s
    ``_make_analyzer_mock()`` explicitly overriding ``.rate_limited``).
    """

    def __init__(self) -> None:
        self._client_id = ""
        self._client_secret = ""  # nosec B105
        self.backup_policy = "archived_only"
        self._session: aiohttp.ClientSession | None = None

        self.connected = False
        self.rate_limited = False
        self.quota_exceeded = False

        self._access_token = ""  # nosec B105
        self._refresh_token = ""  # nosec B105
        self._expires_at = 0.0
        self._account_email = ""
        self._folder_id = ""
        self._folder_name = ""

        self._quota_cache: DriveQuota | None = None
        self._quota_cached_at = 0.0

        # get_or_create_folder_path()'s memo: "<parent_id>/<child name>" ->
        # child folder id. Uploading many clips that share the same date/
        # camera subfolder (the common case — a whole batch from one day)
        # would otherwise re-list-or-create the same folders on every
        # single upload; this makes every one after the first a cache hit.
        self._subfolder_cache: dict[str, str] = {}

        self._load_settings()
        self._load_credentials()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Credential persistence
    # ------------------------------------------------------------------

    def _load_credentials(self) -> None:
        if not CREDENTIALS_FILE.exists():
            return
        try:
            data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            self._access_token = str(data.get("access_token", ""))
            self._refresh_token = str(data.get("refresh_token", ""))
            self._expires_at = float(data.get("expires_at", 0) or 0)
            self._account_email = str(data.get("account_email", ""))
            self._folder_id = str(data.get("folder_id", ""))
            self._folder_name = str(data.get("folder_name", ""))
            self.connected = bool(self._refresh_token)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not load Google Drive credentials: %s", exc)

    def _persist_credentials(self) -> None:
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
            "account_email": self._account_email,
            "folder_id": self._folder_id,
            "folder_name": self._folder_name,
        }
        try:
            # Write to a temp file and rename over the target so a crash
            # mid-write can't leave a truncated/corrupt credentials file
            # behind — os.replace() is atomic on the same filesystem (same
            # convention as downloader.py's _persist_auth()/AUTH_FILE).
            tmp_path = CREDENTIALS_FILE.with_suffix(CREDENTIALS_FILE.suffix + ".tmp")
            tmp_path.write_text(json.dumps(data, indent=2))
            os.replace(tmp_path, CREDENTIALS_FILE)
        except OSError as exc:
            _LOGGER.warning("Could not persist Google Drive credentials: %s", exc)

    def _clear_credentials(self) -> None:
        self._access_token = ""  # nosec B105
        self._refresh_token = ""  # nosec B105
        self._expires_at = 0.0
        self._account_email = ""
        self._folder_id = ""
        self._folder_name = ""
        self.connected = False
        CREDENTIALS_FILE.unlink(missing_ok=True)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    # ------------------------------------------------------------------
    # Settings (OAuth client id/secret + backup policy — edited from the
    # Storage tab, not config.yaml; see media_server.py's
    # _handle_gdrive_settings_get/_put)
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        if not SETTINGS_FILE.exists():
            return
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            self._client_id = str(data.get("client_id", ""))
            self._client_secret = str(data.get("client_secret", ""))
            policy = str(data.get("backup_policy", "archived_only") or "archived_only")
            self.backup_policy = (
                policy if policy in _BACKUP_POLICIES else "archived_only"
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not load Google Drive settings: %s", exc)

    def set_settings(
        self, client_id: str, client_secret: str | None, backup_policy: str
    ) -> None:
        """Update OAuth client id/secret + backup policy and persist them.

        *client_secret* of None (or empty) keeps the previously stored
        secret — the settings form doesn't need to re-collect it on every
        save of an unrelated field like backup_policy. The in-memory update
        above happens before the write below so the change still takes
        immediate effect this session even if persisting it fails; that
        failure propagates to the caller (media_server.py's
        _handle_gdrive_settings_put turns it into a 500) rather than being
        swallowed here, so a user clicking Save gets an honest result
        instead of a false "saved" response.
        """
        self._client_id = client_id.strip()
        if client_secret:
            self._client_secret = client_secret.strip()
        self.backup_policy = (
            backup_policy if backup_policy in _BACKUP_POLICIES else "archived_only"
        )
        SETTINGS_FILE.write_text(
            json.dumps(
                {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "backup_policy": self.backup_policy,
                },
                indent=2,
            )
        )

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def has_client_secret(self) -> bool:
        return bool(self._client_secret)

    @property
    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def account_email(self) -> str:
        return self._account_email

    @property
    def folder_id(self) -> str:
        return self._folder_id

    @property
    def folder_name(self) -> str:
        return self._folder_name

    # ------------------------------------------------------------------
    # Device flow
    # ------------------------------------------------------------------

    async def start_device_flow(self) -> DeviceFlowInfo | None:
        """Begin a device-flow login. Returns None (logged) on failure."""
        if not self.is_configured:
            _LOGGER.warning(
                "Cannot start Google Drive device flow — client id/secret not set"
            )
            return None
        try:
            session = self._get_session()
            async with session.post(
                _DEVICE_CODE_URL,
                data={"client_id": self._client_id, "scope": _SCOPE},
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    _LOGGER.warning(
                        "Could not start Google Drive device flow: HTTP %d %s",
                        resp.status,
                        data,
                    )
                    return None
        except _JSON_ERRORS as exc:
            _LOGGER.warning("Could not start Google Drive device flow: %s", exc)
            return None

        return DeviceFlowInfo(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_url=data.get("verification_url")
            or data.get("verification_uri", ""),
            expires_in=int(data.get("expires_in", 1800)),
            interval=int(data.get("interval", 5)),
        )

    async def poll_once_for_token(self, device_code: str) -> TokenPollResult:
        """Poll once for the device-flow token. Never raises."""
        try:
            session = self._get_session()
            async with session.post(
                _TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                data = await resp.json()
                status = resp.status
        except _JSON_ERRORS as exc:
            return TokenPollResult(status="error", message=str(exc))

        if status == 200:
            self._access_token = str(data.get("access_token", ""))
            if data.get("refresh_token"):
                self._refresh_token = str(data["refresh_token"])
            self._expires_at = time.time() + float(data.get("expires_in", 3600))
            self.connected = bool(self._refresh_token)
            await self._fetch_account_email()
            self._persist_credentials()
            return TokenPollResult(status="success")

        error = str(data.get("error", ""))
        if error in (
            "authorization_pending",
            "slow_down",
            "expired_token",
            "access_denied",
        ):
            # The "expired" status goes through a named constant: a string
            # literal against the "expired_token" key reads to bandit (B105)
            # as a hardcoded credential, and a nosec inside a multi-line dict
            # sprays spurious per-line "unused nosec" warnings.
            expired_status: PollStatus = "expired"
            mapped: dict[str, PollStatus] = {
                "authorization_pending": "pending",
                "slow_down": "slow_down",
                "expired_token": expired_status,
                "access_denied": "denied",
            }
            return TokenPollResult(status=mapped[error])
        return TokenPollResult(
            status="error", message=str(data.get("error_description") or error)
        )

    async def _fetch_account_email(self) -> None:
        try:
            session = self._get_session()
            async with session.get(
                f"{_DRIVE_API}/about",
                params={"fields": "user"},
                headers=self._auth_headers(),
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._account_email = str(
                        data.get("user", {}).get("emailAddress", "")
                    )
        except _JSON_ERRORS as exc:
            _LOGGER.debug("Could not fetch Google Drive account email: %s", exc)

    async def _ensure_valid_token(self) -> bool:
        """Refresh the access token if near/past expiry. Returns True if usable."""
        if not self._refresh_token:
            return False
        if self._access_token and time.time() < self._expires_at - 60:
            return True

        try:
            session = self._get_session()
            async with session.post(
                _TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                data = await resp.json()
                status = resp.status
        except _JSON_ERRORS as exc:
            _LOGGER.warning("Google Drive token refresh failed: %s", exc)
            return False

        if status != 200:
            if str(data.get("error", "")) == "invalid_grant":
                # The refresh token itself was revoked/expired — the
                # connection is dead. Force a clean "disconnected" state
                # (matching Blink's own hard-failure symmetry) rather than a
                # confusing half-connected limbo that keeps retrying forever.
                _LOGGER.warning("Google Drive refresh token invalid — disconnecting")
                self._clear_credentials()
            else:
                _LOGGER.warning("Google Drive token refresh failed: %s", data)
            return False

        self._access_token = str(data.get("access_token", ""))
        # Google's refresh-grant response frequently omits refresh_token
        # entirely (only occasionally rotates it) — only overwrite ours if a
        # new one was actually returned, or the *next* refresh would find
        # nothing to send and silently brick the connection.
        if data.get("refresh_token"):
            self._refresh_token = str(data["refresh_token"])
        self._expires_at = time.time() + float(data.get("expires_in", 3600))
        self._persist_credentials()
        return True

    async def disconnect(self) -> None:
        """Best-effort revoke, then always clear local credentials."""
        token = self._refresh_token or self._access_token
        if token:
            try:
                session = self._get_session()
                # The response body is never needed -- this is a
                # fire-and-forget revoke. `async with` (rather than a bare
                # await) is what makes aiohttp release the connection
                # properly; there's nothing else to do with the response.
                async with session.post(
                    _REVOKE_URL, data={"token": token}, timeout=_HTTP_TIMEOUT
                ):
                    pass  # NOSONAR
            except _HTTP_ERRORS as exc:
                _LOGGER.warning(
                    "Could not revoke Google Drive token (disconnecting locally anyway): %s",
                    exc,
                )
        self._clear_credentials()

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    async def list_folders(self, parent_id: str = "root") -> list[DriveFolder]:
        """List the non-trashed folders directly inside *parent_id*.

        Defaults to the Drive root, so the folder browser can drill down one
        level at a time (breadcrumb navigation) rather than showing every
        folder in the account flattened into one list.
        """
        if not await self._ensure_valid_token():
            return []
        try:
            session = self._get_session()
            async with session.get(
                f"{_DRIVE_API}/files",
                params={
                    "q": (
                        f"mimeType='{_FOLDER_MIME_TYPE}' and trashed=false "
                        f"and '{parent_id}' in parents"
                    ),
                    "fields": "files(id,name,modifiedTime)",
                    "pageSize": "200",
                    "orderBy": "name",
                },
                headers=self._auth_headers(),
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status == 429:
                    self.rate_limited = True
                if resp.status != 200:
                    return []
                data = await resp.json()
        except _JSON_ERRORS as exc:
            _LOGGER.warning("Could not list Google Drive folders: %s", exc)
            return []

        return [
            DriveFolder(
                id=str(f["id"]),
                name=str(f.get("name", "")),
                modified_time=str(f.get("modifiedTime", "")),
            )
            for f in data.get("files", [])
        ]

    async def create_folder(
        self, name: str, parent_id: str = "root"
    ) -> DriveFolder | None:
        """Create a new folder inside *parent_id*. Does not select it as the
        active backup destination — see select_folder."""
        if not await self._ensure_valid_token():
            return None
        try:
            session = self._get_session()
            async with session.post(
                f"{_DRIVE_API}/files",
                json={
                    "name": name,
                    "mimeType": _FOLDER_MIME_TYPE,
                    "parents": [parent_id],
                },
                params={"fields": "id,name,modifiedTime"},
                headers=self._auth_headers(),
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.warning(
                        "Could not create Google Drive folder %r: HTTP %d",
                        name,
                        resp.status,
                    )
                    return None
                data = await resp.json()
        except _JSON_ERRORS as exc:
            _LOGGER.warning("Could not create Google Drive folder %r: %s", name, exc)
            return None

        return DriveFolder(
            id=str(data["id"]),
            name=str(data.get("name", name)),
            modified_time=str(data.get("modifiedTime", "")),
        )

    def select_folder(self, folder_id: str, folder_name: str) -> None:
        """Set the active backup destination folder and persist the choice."""
        self._folder_id = folder_id
        self._folder_name = folder_name
        self._persist_credentials()

    async def get_or_create_folder_path(
        self, path_parts: list[str], root_id: str | None = None
    ) -> str | None:
        """Resolve a nested folder path under *root_id*, creating any
        folder along the way that doesn't already exist.

        E.g. ``["2026-06-05", "Driveway"]`` under the connected backup
        folder resolves to (creating first if needed)
        ``<backup folder>/2026-06-05/Driveway``, returning that innermost
        folder's id — this is what gives clip backups their
        date-then-camera structure in Drive (see ``GDriveUploadQueue``)
        instead of dumping everything flat into one folder. Generic over
        *any* nested path, not clip-specific, so it's reusable for other
        folder-organizing needs. Returns None (logged by the underlying
        list/create calls) if no root is available or any step fails —
        callers should treat that as "could not resolve, don't upload
        into an unknown location" rather than falling back to the root.
        """
        parent = root_id or self._folder_id
        if not parent:
            return None
        cache_prefix = parent
        for part in path_parts:
            cache_key = f"{cache_prefix}/{part}"
            cached = self._subfolder_cache.get(cache_key)
            if cached:
                parent = cached
                cache_prefix = cache_key
                continue

            existing = next(
                (f for f in await self.list_folders(parent) if f.name == part), None
            )
            if existing is not None:
                parent = existing.id
            else:
                created = await self.create_folder(part, parent_id=parent)
                if created is None:
                    return None
                parent = created.id

            self._subfolder_cache[cache_key] = parent
            cache_prefix = cache_key
        return parent

    # ------------------------------------------------------------------
    # Upload / delete / quota
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        local_path: Path,
        remote_name: str,
        content_type: str = "video/mp4",
        folder_id: str | None = None,
    ) -> str | None:
        """Upload a file into *folder_id* (default: the connected default
        folder). Returns the new Drive file id.

        *folder_id* lets a manual, one-off upload (e.g. Library's "Upload to
        Drive" bulk action) target a different folder than the one used for
        automatic archived-clip backups, without changing that default.

        Uses the resumable-upload endpoint for a single whole-file PUT (correct
        and simple for typical clip sizes) rather than persisted chunk-by-chunk
        resumption — true crash-resumable upload is a possible future
        enhancement, not required for this to work correctly.
        """
        # Reset per-attempt flags at the start of each upload (mirrors
        # BaseAnalyzer._reset_analysis_state's _last_rate_limited reset) so a
        # rate limit/quota error from a previous attempt doesn't permanently
        # block every future upload — only *this* attempt's outcome counts.
        self.rate_limited = False
        self.quota_exceeded = False

        if not await self._ensure_valid_token():
            return None
        target_folder = folder_id or self._folder_id
        if not target_folder:
            _LOGGER.warning(
                "Cannot upload %s — no Google Drive folder selected", remote_name
            )
            return None

        size = local_path.stat().st_size
        metadata = {"name": remote_name, "parents": [target_folder]}

        try:
            session = self._get_session()
            async with session.post(
                _DRIVE_UPLOAD_API,
                params={"uploadType": "resumable"},
                json=metadata,
                headers={
                    **self._auth_headers(),
                    "X-Upload-Content-Type": content_type,
                    "X-Upload-Content-Length": str(size),
                },
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status == 429:
                    self.rate_limited = True
                if resp.status == 403:
                    try:
                        body = await resp.json()
                    except _JSON_ERRORS:
                        body = {}
                    self._note_quota_or_rate_limit(body)
                if resp.status != 200:
                    _LOGGER.warning(
                        "Could not start Google Drive upload for %s: HTTP %d",
                        remote_name,
                        resp.status,
                    )
                    return None
                session_uri = resp.headers.get("Location")

            if not session_uri:
                _LOGGER.warning(
                    "Google Drive upload init for %s returned no session URI",
                    remote_name,
                )
                return None

            async with aiofiles.open(local_path, "rb") as fh:
                file_bytes = await fh.read()

            async with session.put(
                session_uri,
                data=file_bytes,
                headers={"Content-Length": str(size)},
                timeout=_UPLOAD_TIMEOUT,
            ) as put_resp:
                if put_resp.status not in (200, 201):
                    _LOGGER.warning(
                        "Google Drive upload PUT for %s failed: HTTP %d",
                        remote_name,
                        put_resp.status,
                    )
                    return None
                result = await put_resp.json()
        except _JSON_ERRORS as exc:
            _LOGGER.warning("Google Drive upload failed for %s: %s", remote_name, exc)
            return None

        return str(result.get("id", "")) or None

    def _note_quota_or_rate_limit(self, body: dict[str, Any]) -> None:
        reasons = {e.get("reason", "") for e in body.get("error", {}).get("errors", [])}
        if any("quota" in r.lower() for r in reasons):
            self.quota_exceeded = True
        elif any("ratelimit" in r.lower() for r in reasons):
            self.rate_limited = True

    async def delete_file(self, file_id: str) -> bool:
        """Move a file to Drive's trash (recoverable for ~30 days).

        Trash, not permanent delete — deleting a clip from our UI shouldn't
        make its Drive backup unrecoverable too.
        """
        if not await self._ensure_valid_token():
            return False
        try:
            session = self._get_session()
            async with session.patch(
                f"{_DRIVE_API}/files/{file_id}",
                json={"trashed": True},
                headers=self._auth_headers(),
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status == 429:
                    self.rate_limited = True
                return resp.status == 200
        except _HTTP_ERRORS as exc:
            _LOGGER.warning("Could not trash Google Drive file %s: %s", file_id, exc)
            return False

    async def get_quota(self) -> DriveQuota | None:
        now = time.time()
        if self._quota_cache and now - self._quota_cached_at < _QUOTA_CACHE_SECONDS:
            return self._quota_cache
        if not await self._ensure_valid_token():
            return None

        try:
            session = self._get_session()
            async with session.get(
                f"{_DRIVE_API}/about",
                params={"fields": "storageQuota"},
                headers=self._auth_headers(),
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except _JSON_ERRORS as exc:
            _LOGGER.warning("Could not fetch Google Drive quota: %s", exc)
            return None

        quota_data = data.get("storageQuota", {})
        quota = DriveQuota(
            limit=int(quota_data["limit"]) if quota_data.get("limit") else None,
            usage=int(quota_data.get("usage", 0) or 0),
            usage_in_drive=int(quota_data.get("usageInDrive", 0) or 0),
        )
        self._quota_cache = quota
        self._quota_cached_at = now
        return quota
