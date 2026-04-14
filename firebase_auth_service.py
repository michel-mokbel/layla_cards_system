"""
firebase_auth_service.py - Firebase Authentication helpers for Streamlit login flows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Callable, Optional
from urllib import parse, request
from urllib.error import HTTPError


RESTPostJSON = Callable[[str, dict[str, Any]], dict[str, Any]]
RESTPostForm = Callable[[str, dict[str, str]], dict[str, Any]]


@dataclass(frozen=True)
class FirebaseAuthSession:
    uid: str
    email: str
    id_token: str
    refresh_token: str
    expires_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FirebaseAuthSession":
        return cls(
            uid=str(payload.get("uid", "")).strip(),
            email=str(payload.get("email", "")).strip(),
            id_token=str(payload.get("id_token", "")).strip(),
            refresh_token=str(payload.get("refresh_token", "")).strip(),
            expires_at=str(payload.get("expires_at", "")).strip(),
        )


def sign_in_with_email_password(
    api_key: str,
    email: str,
    password: str,
    *,
    post_json: Optional[RESTPostJSON] = None,
) -> FirebaseAuthSession:
    cleaned_email = str(email or "").strip()
    if not api_key:
        raise RuntimeError("Firebase Web API key is missing.")
    if not cleaned_email or not password:
        raise ValueError("Email and password are required.")

    payload = {
        "email": cleaned_email,
        "password": password,
        "returnSecureToken": True,
    }
    response = (post_json or _post_json)(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}",
        payload,
    )
    return FirebaseAuthSession(
        uid=str(response.get("localId", "")).strip(),
        email=str(response.get("email", cleaned_email)).strip(),
        id_token=str(response.get("idToken", "")).strip(),
        refresh_token=str(response.get("refreshToken", "")).strip(),
        expires_at=_expires_at_from_seconds(response.get("expiresIn", "3600")),
    )


def refresh_id_token(
    api_key: str,
    refresh_token: str,
    *,
    post_form: Optional[RESTPostForm] = None,
    email: str = "",
) -> FirebaseAuthSession:
    if not api_key:
        raise RuntimeError("Firebase Web API key is missing.")
    if not refresh_token:
        raise ValueError("Refresh token is required.")

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    response = (post_form or _post_form)(
        f"https://securetoken.googleapis.com/v1/token?key={api_key}",
        payload,
    )
    return FirebaseAuthSession(
        uid=str(response.get("user_id", "")).strip(),
        email=str(email or "").strip(),
        id_token=str(response.get("id_token", "")).strip(),
        refresh_token=str(response.get("refresh_token", refresh_token)).strip(),
        expires_at=_expires_at_from_seconds(response.get("expires_in", "3600")),
    )


def auth_session_expiring(session: FirebaseAuthSession, *, within_seconds: int = 120) -> bool:
    expires_at = _parse_datetime(session.expires_at)
    return expires_at <= datetime.now(timezone.utc) + timedelta(seconds=within_seconds)


def firebase_auth_error_message(exc: Exception) -> str:
    text = str(exc)
    mappings = {
        "EMAIL_NOT_FOUND": "No Firebase user exists for that email.",
        "INVALID_PASSWORD": "The password is incorrect.",
        "INVALID_LOGIN_CREDENTIALS": "Email or password is incorrect.",
        "USER_DISABLED": "This Firebase user account has been disabled.",
        "TOKEN_EXPIRED": "Your session has expired. Please sign in again.",
        "INVALID_REFRESH_TOKEN": "Your session is no longer valid. Please sign in again.",
        "PROJECT_NUMBER_MISMATCH": "Firebase API key and refresh token belong to different projects.",
        "API key not valid": "Firebase Web API key is invalid.",
    }
    for marker, message in mappings.items():
        if marker in text:
            return message
    return text or "Authentication failed."


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(_parse_http_error(exc)) from exc


def _post_form(url: str, payload: dict[str, str]) -> dict[str, Any]:
    data = parse.urlencode(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(_parse_http_error(exc)) from exc


def _parse_http_error(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return f"Firebase Auth request failed ({exc.code})."

    error_obj = payload.get("error", {}) if isinstance(payload, dict) else {}
    if isinstance(error_obj, dict):
        message = str(error_obj.get("message", "")).strip()
        if message:
            return message
    return f"Firebase Auth request failed ({exc.code})."


def _expires_at_from_seconds(expires_in: object) -> str:
    try:
        seconds = int(float(expires_in))
    except Exception:
        seconds = 3600
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(seconds - 60, 60))
    return expires_at.replace(microsecond=0).isoformat()


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.now(timezone.utc)
