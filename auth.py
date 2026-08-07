from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException, Request, Response


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SESSION_COOKIE = "jewelry_session"
SESSION_DAYS = 7
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_LENGTH = 32


@dataclass(frozen=True)
class User:
    id: int
    username: str


class AuthStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires
                    ON sessions(expires_at);
                """
            )

    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_KEY_LENGTH,
        )
        return "scrypt$%d$%d$%d$%s$%s" % (
            SCRYPT_N,
            SCRYPT_R,
            SCRYPT_P,
            salt.hex(),
            digest.hex(),
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            scheme, n, r, p, salt_hex, digest_hex = encoded.split("$", 5)
            if scheme != "scrypt":
                return False
            digest = hashlib.scrypt(
                password.encode("utf-8"),
                salt=bytes.fromhex(salt_hex),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(bytes.fromhex(digest_hex)),
            )
            return hmac.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def validate_username(username: str) -> str:
        username = username.strip()
        if not USERNAME_RE.fullmatch(username):
            raise HTTPException(status_code=400, detail="用户名需为3-32位字母、数字、下划线、点或短横线")
        return username

    @staticmethod
    def validate_password(password: str) -> str:
        if len(password) < 8 or len(password) > 128:
            raise HTTPException(status_code=400, detail="密码长度必须为8-128位")
        return password

    def register(self, username: str, password: str) -> User:
        username = self.validate_username(username)
        password = self.validate_password(password)
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, self.hash_password(password), created_at),
                )
                return User(int(cursor.lastrowid), username)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="用户名已存在") from exc

    def authenticate(self, username: str, password: str) -> User:
        username = username.strip()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None or not self.verify_password(password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        return User(int(row["id"]), str(row["username"]))

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(48)
        expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE expires_at < ?",
                (datetime.now(timezone.utc).isoformat(),),
            )
            connection.execute(
                "DELETE FROM sessions WHERE user_id = ?",
                (user_id,),
            )
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (self._hash_token(token), user_id, expires_at.isoformat()),
            )
        return token

    def user_for_token(self, token: str | None) -> User | None:
        if not token:
            return None
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.username
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (self._hash_token(token), now),
            ).fetchone()
        return None if row is None else User(int(row["id"]), str(row["username"]))

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash_token(token),))

    def clear_sessions(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions")


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(request: Request, store: AuthStore) -> User:
    user = store.user_for_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user
