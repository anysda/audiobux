"""Sign-in to the web process: one user, `admin`, and a session in a cookie.

Of the password only its argon2 hash is kept, and of a session only the SHA-256
of its token, in `<data>/web/auth.db`: what the cookie carries is in no file.
`audiobux password` sets the password (install.sh does at the first start); setting
it again closes every session. There is no sign-up, no reset by mail and no second
factor: fastapi-users serves only its sign-in and sign-out. Wrong passwords are
counted by the client's address (`Attempts`).
"""

import hashlib
import math
import secrets
import sqlite3
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers, IntegerIDMixin
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users.db import BaseUserDatabase
from fastapi_users.password import PasswordHelper

LOGIN = "admin"
# a session lasts this long from the sign-in, seconds
LIFETIME = 30 * 24 * 3600
# sign-ins from one address in a row without a right one, and how long it is shut out after them, seconds
ATTEMPTS = 5
LOCKOUT = 15 * 60
# the shortest password, in characters
PASSWORD_MIN = 12
SCHEMA = """
create table if not exists user (id integer primary key check (id = 1), hashed_password text not null);
create table if not exists session (hash text primary key, user_id integer not null, created_at real not null);
-- the sessions of before, kept by the token itself: whoever had one signs in again
drop table if exists token;
"""


@dataclass
class User:
    id: int
    hashed_password: str
    email: str = LOGIN
    is_active: bool = True
    is_superuser: bool = False
    is_verified: bool = True


@dataclass
class Token:
    token: str
    user_id: int
    created_at: datetime


class Store:
    """The user and the sessions, one connection for the threads, taken in turn."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # opened on first use, so that making the app (for its API description) touches no disk
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def run(self, sql: str, args: tuple[object, ...] = ()) -> list[Any]:
        with self._lock:
            if self._db is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # the hash and the sessions are for the service alone
                self.path.touch(0o600)
                self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                self._db.executescript(SCHEMA)
            return self._db.execute(sql, args).fetchall()


class Users(BaseUserDatabase[User, int]):
    def __init__(self, store: Store) -> None:
        self.store = store

    async def get(self, id: int) -> User | None:
        rows = self.store.run("select hashed_password from user where id = ?", (id,))
        return User(id, rows[0][0]) if rows else None

    async def get_by_email(self, email: str) -> User | None:
        return await self.get(1) if email == LOGIN else None

    async def update(self, user: User, update_dict: dict[str, Any]) -> User:
        # the sign-in hashes the password anew when the hash's parameters are old
        if "hashed_password" in update_dict:
            user.hashed_password = update_dict["hashed_password"]
            self.store.run("update user set hashed_password = ? where id = ?", (user.hashed_password, user.id))
        return user


class Tokens:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def get_by_token(self, token: str, max_age: datetime | None = None) -> Token | None:
        since = max_age.timestamp() if max_age else 0
        rows = self.store.run(
            "select user_id, created_at from session where hash = ? and created_at >= ?", (_hash(token), since)
        )
        return Token(token, rows[0][0], datetime.fromtimestamp(rows[0][1], UTC)) if rows else None

    async def create(self, create_dict: dict[str, Any]) -> Token:
        now = datetime.now(UTC)
        token = Token(create_dict["token"], create_dict["user_id"], now)
        self.store.run("delete from session where created_at < ?", (now.timestamp() - LIFETIME,))
        self.store.run("insert into session values (?, ?, ?)", (_hash(token.token), token.user_id, now.timestamp()))
        return token

    async def update(self, access_token: Token, update_dict: dict[str, Any]) -> Token:
        return access_token

    async def delete(self, access_token: Token) -> None:
        self.store.run("delete from session where hash = ?", (_hash(access_token.token),))


class Attempts:
    """A dependency of the sign-in: after `ATTEMPTS` sign-ins from an address without a
    right one, each within `LOCKOUT` of the one before, the address gets 429 for `LOCKOUT`.
    A sign-in is counted before the password is checked, so that ones sent at once count
    too; a right one forgets them. Kept by the process: a restart forgets them all."""

    clock = staticmethod(time.monotonic)

    def __init__(self) -> None:
        self.tried: dict[str, tuple[int, float]] = {}

    async def __call__(self, request: Request) -> AsyncIterator[None]:
        if not request.url.path.endswith("/login"):  # the sign-out of the same router
            yield
            return
        address = request.client.host if request.client else ""
        now = self.clock()
        count, last = self.tried.get(address, (0, now))
        if now - last >= LOCKOUT:
            count = 0
        if count >= ATTEMPTS:
            wait = math.ceil(LOCKOUT - (now - last))
            raise HTTPException(429, "too-many-attempts", headers={"retry-after": str(wait)})
        # the addresses tried long ago go, so that many of them do not pile up
        self.tried = {a: t for a, t in self.tried.items() if now - t[1] < LOCKOUT}
        self.tried[address] = (count + 1, now)
        yield
        self.tried.pop(address, None)


class Manager(IntegerIDMixin, BaseUserManager[User, int]):
    # for the reset and verification routes, which are not served
    reset_password_token_secret = verification_token_secret = secrets.token_urlsafe()


def users(path: Path) -> tuple[FastAPIUsers[User, int], AuthenticationBackend[User, int]]:
    store = Store(path)
    manager = Manager(Users(store), PasswordHelper())
    # Lax: a link to the service from elsewhere opens it signed in; changes need its own Origin (web.Guard)
    cookie = CookieTransport("audiobux", cookie_max_age=LIFETIME, cookie_secure=True, cookie_samesite="lax")
    backend = AuthenticationBackend[User, int]("cookie", cookie, lambda: DatabaseStrategy(Tokens(store), LIFETIME))
    return FastAPIUsers[User, int](lambda: manager, [backend]), backend


def set_password(path: Path, password: str, keep: bool = False) -> bool:
    """Keeps the password's hash and closes every session; with `keep`, a password set
    before stays. Whether it was set; a password shorter than `PASSWORD_MIN` is a ValueError."""
    if len(password) < PASSWORD_MIN:
        raise ValueError(f"at least {PASSWORD_MIN} characters")
    store = Store(path)
    if keep and store.run("select 1 from user"):
        return False
    store.run(
        "insert into user values (1, ?) on conflict (id) do update set hashed_password = excluded.hashed_password",
        (PasswordHelper().hash(password),),
    )
    store.run("delete from session")
    return True


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
