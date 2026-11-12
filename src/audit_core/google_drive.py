"""Google Drive, as the Project's own Google account (product owner 2026-11-12).

"Can it be done with Google Drive only -- I don't want to introduce more
complexity": a plain Google account, no Workspace and no shared drive. In a plain
Drive only a file's owner can delete it and a service account can own nothing,
so Verigence does not use a service account: the account's owner approves once
on Google's own screen (OAuth, offline access) and Verigence then acts as that
account -- it makes the folders, reads what the PCs put in them, and moves a
closed journey's folder to the archive (nothing is deleted).

What is kept is the refresh token, sealed with a key made from the OAuth client
secret (the secret is in the service's variables, never in the database), so the
database alone opens nothing.

Configuration (the feature is off until all are set):
    AUDIT_CORE_GOOGLE_DRIVE_CLIENT_ID / _CLIENT_SECRET   the OAuth client (type: web application)
    AUDIT_CORE_GOOGLE_DRIVE_REDIRECT_URI                the client's authorised redirect URI
    AUDIT_CORE_GOOGLE_DRIVE_RETURN_URL                  where the browser lands after approving

This module only talks to Google; what the folders and files mean is
uc03_p2_drive_intake's.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

SCOPE = "https://www.googleapis.com/auth/drive"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
FOLDER_TYPE = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,size,md5Checksum,createdTime,modifiedTime,parents,trashed"
DEFAULT_REDIRECT_URI = "https://verigence.jbrconsulting.in/audit-core/p2/v1/integrations/google-drive/callback"
DEFAULT_RETURN_URL = "https://verigence.jbrconsulting.in/p2/drive-uploads"
_STATE_SECONDS = 900
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class DriveError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Google Drive {status}: {detail}"[:500])
        self.status = status


class DriveNotFound(DriveError):
    """The file or folder is gone (or was never this account's)."""


class DriveAuthorizationLost(DriveError):
    """Google no longer accepts the stored approval: the account must connect again."""


@dataclass(frozen=True)
class DriveSettings:
    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    return_url: str = DEFAULT_RETURN_URL


def drive_settings() -> DriveSettings | None:
    """The OAuth client from the service's variables; None while the feature is off."""
    client_id = os.environ.get("AUDIT_CORE_GOOGLE_DRIVE_CLIENT_ID", "").strip()
    secret = os.environ.get("AUDIT_CORE_GOOGLE_DRIVE_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        return None
    return DriveSettings(
        client_id=client_id, client_secret=secret,
        redirect_uri=os.environ.get("AUDIT_CORE_GOOGLE_DRIVE_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI,
        return_url=os.environ.get("AUDIT_CORE_GOOGLE_DRIVE_RETURN_URL", "").strip() or DEFAULT_RETURN_URL,
    )


# --------------------------------------------------------------------------- sealing and state

def _fernet(settings: DriveSettings) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(f"drive-token:{settings.client_secret}".encode()).digest()))


def seal(settings: DriveSettings, value: str) -> str:
    return _fernet(settings).encrypt(value.encode()).decode()


def unseal(settings: DriveSettings, sealed: str) -> str:
    try:
        return _fernet(settings).decrypt(sealed.encode()).decode()
    except InvalidToken as exc:  # the client secret changed since the account connected
        raise DriveAuthorizationLost(401, "The stored Google approval cannot be opened; connect the account again.") from exc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_state(settings: DriveSettings, *, tenant_id: str, actor_id: str, now: float | None = None) -> str:
    """What Google hands back with the approval: which Project asked, who, and until when --
    signed, so the callback (which carries no Verigence login) trusts nothing else."""
    body = _b64(json.dumps({"t": tenant_id, "a": actor_id, "e": int((now or time.time()) + _STATE_SECONDS)},
                           separators=(",", ":")).encode())
    mac = hmac.new(f"drive-state:{settings.client_secret}".encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def read_state(settings: DriveSettings, state: str, *, now: float | None = None) -> dict[str, str] | None:
    """The tenant and actor of a state this service signed and that has not expired; else None."""
    body, sep, mac = (state or "").partition(".")
    if not sep:
        return None
    expected = hmac.new(f"drive-state:{settings.client_secret}".encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _unb64(mac)):
            return None
        data = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or int(data.get("e") or 0) < (now or time.time()):
        return None
    return {"tenant_id": str(data.get("t") or ""), "actor_id": str(data.get("a") or "")}


def consent_url(settings: DriveSettings, state: str) -> str:
    """Google's approval screen. ``prompt=consent`` so a refresh token always comes back,
    also when the account approved before."""
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.client_id, "redirect_uri": settings.redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
    })


def exchange_code(settings: DriveSettings, code: str, *, http: httpx.Client | None = None) -> dict[str, Any]:
    """The approval code for its tokens (``refresh_token`` is the one kept)."""
    client = http or httpx.Client(timeout=_TIMEOUT)
    try:
        response = client.post(TOKEN_URL, data={
            "client_id": settings.client_id, "client_secret": settings.client_secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": settings.redirect_uri,
        })
    finally:
        if http is None:
            client.close()
    if response.status_code != 200:
        raise DriveError(response.status_code, _problem(response))
    return response.json()


def _problem(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(data.get("error_description") or error or data)[:300]


# --------------------------------------------------------------------------- the client

class DriveClient:
    """The Drive of one connected account. Every call is the account's own: what it lists
    is the account's Drive, what it makes the account owns."""

    def __init__(self, settings: DriveSettings, refresh_token: str, *, http: httpx.Client | None = None) -> None:
        self._settings = settings
        self._refresh_token = refresh_token
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._access_token: str | None = None
        self._expires_at = 0.0

    def close(self) -> None:
        self._http.close()

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        response = self._http.post(TOKEN_URL, data={
            "client_id": self._settings.client_id, "client_secret": self._settings.client_secret,
            "refresh_token": self._refresh_token, "grant_type": "refresh_token",
        })
        if response.status_code in (400, 401):
            raise DriveAuthorizationLost(response.status_code, _problem(response))
        if response.status_code != 200:
            raise DriveError(response.status_code, _problem(response))
        data = response.json()
        self._access_token = str(data["access_token"])
        self._expires_at = time.time() + int(data.get("expires_in") or 3600)
        return self._access_token

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None) -> httpx.Response:
        response = self._http.request(method, f"{API}{path}", params=params, json=body,
                                      headers={"Authorization": f"Bearer {self._token()}"})
        if response.status_code == 404:
            raise DriveNotFound(404, _problem(response))
        if response.status_code == 401:
            raise DriveAuthorizationLost(401, _problem(response))
        if response.status_code >= 400:
            raise DriveError(response.status_code, _problem(response))
        return response

    def account_email(self) -> str | None:
        user = self._request("GET", "/about", params={"fields": "user(emailAddress)"}).json().get("user") or {}
        return user.get("emailAddress")

    def start_page_token(self) -> str:
        return str(self._request("GET", "/changes/startPageToken").json()["startPageToken"])

    def changes(self, page_token: str) -> tuple[list[dict[str, Any]], str]:
        """Everything added, changed or removed in the account's Drive since ``page_token``,
        and the token to ask from next time. This is how a file is found whatever folder it
        was put in and whatever date it carries: nothing is walked, so it costs the same
        however large the Drive grows."""
        out: list[dict[str, Any]] = []
        token = page_token
        while True:
            data = self._request("GET", "/changes", params={
                "pageToken": token, "pageSize": 1000, "spaces": "drive", "restrictToMyDrive": "true",
                "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))",
            }).json()
            out.extend(data.get("changes") or [])
            if data.get("nextPageToken"):
                token = str(data["nextPageToken"])
                continue
            return out, str(data.get("newStartPageToken") or token)

    def get(self, file_id: str) -> dict[str, Any]:
        return self._request("GET", f"/files/{file_id}", params={"fields": FILE_FIELDS}).json()

    def children(self, folder_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "q": f"'{folder_id}' in parents and trashed = false", "pageSize": 1000,
                "fields": f"nextPageToken,files({FILE_FIELDS})", "spaces": "drive",
            }
            if token:
                params["pageToken"] = token
            data = self._request("GET", "/files", params=params).json()
            out.extend(data.get("files") or [])
            token = data.get("nextPageToken")
            if not token:
                return out

    def create_folder(self, name: str, parent_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_TYPE}
        if parent_id:
            body["parents"] = [parent_id]
        return self._request("POST", "/files", params={"fields": FILE_FIELDS}, body=body).json()

    def move(self, file_id: str, new_parent_id: str, old_parent_id: str | None) -> None:
        """To another folder of the account's Drive (its id, and so its record, stay the same)."""
        params: dict[str, Any] = {"addParents": new_parent_id, "fields": "id,parents"}
        if old_parent_id:
            params["removeParents"] = old_parent_id
        self._request("PATCH", f"/files/{file_id}", params=params, body={})

    def download(self, file_id: str) -> bytes:
        return self._request("GET", f"/files/{file_id}", params={"alt": "media"}).content
