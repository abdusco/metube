from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from functools import wraps
from http import HTTPStatus
from pathlib import Path

from bottle import Bottle, HTTPResponse, abort, redirect, request, static_file


def without_auth(func):
    """Marks a route as publicly accessible — no session cookie required."""
    setattr(func, "_without_auth", True)
    return func


@dataclass
class AuthPlugin:
    """Bottle plugin that gates all routes behind a signed session cookie.

    Routes decorated with @without_auth or registered with skip=[self.name] are exempt.
    All unauthenticated requests to protected routes return 401.

    Cookie format: ``{username}:{HMAC-SHA256(secret+password, username)}``.
    Changing a user's password immediately invalidates their existing cookie.
    """

    users: dict[str, str]
    secret: str
    base_dir: Path
    session_duration: int = 30 * 24 * 3600

    name = "auth"
    api = 2

    def setup(self, app: Bottle) -> None:
        app.route("/login", method="GET", callback=self._login_page)
        app.route("/login", method="POST", callback=self._login_submit)
        app.route("/logout", method="POST", callback=self._logout)

    def apply(self, callback, route):
        if getattr(callback, "_without_auth", False):
            return callback

        @wraps(callback)
        def _wrapped(*args, **kwargs):
            if self._check_cookie():
                return callback(*args, **kwargs)
            abort(401, "Authentication required")

        return _wrapped

    def _make_token(self, username: str) -> str:
        key = f"{self.secret}:{self.users[username]}".encode()
        return hmac.new(key, username.encode(), hashlib.sha256).hexdigest()

    def _check_cookie(self) -> bool:
        raw = request.get_cookie("session") or ""
        if ":" not in raw:
            return False
        username, _, sig = raw.partition(":")
        if username not in self.users:
            return False
        return hmac.compare_digest(sig, self._make_token(username))

    def _valid_credentials(self, username: str, password: str) -> bool:
        expected = self.users.get(username)
        if not expected:
            return False
        return hmac.compare_digest(password, expected)

    @without_auth
    def _login_page(self):
        return static_file("login.html", root=str(self.base_dir / "ui"))

    @without_auth
    def _logout(self):
        resp = HTTPResponse(status=HTTPStatus.SEE_OTHER)
        resp.set_header("Location", "/")
        resp.delete_cookie("session", path="/")
        raise resp

    @without_auth
    def _login_submit(self):
        username = request.forms.get("username", "").strip()
        password = request.forms.get("password", "")
        if not self._valid_credentials(username, password):
            redirect("/login?error=invalid_credentials")
        token = f"{username}:{self._make_token(username)}"
        resp = HTTPResponse(status=HTTPStatus.SEE_OTHER)
        resp.set_header("Location", "/")
        resp.set_cookie(
            "session",
            token,
            httponly=True,
            samesite="Strict",
            path="/",
            max_age=self.session_duration,
        )
        raise resp
