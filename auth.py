from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response


SESSION_COOKIE = "lookbook_session"
ADMIN_SESSION_COOKIE = "lookbook_admin_session"
OAUTH_STATE_COOKIE = "wechat_oauth_state"
SESSION_DAYS = 7
ADMIN_SESSION_HOURS = 12


class ClosingSQLiteConnection(sqlite3.Connection):
    """Make ``with store.connect()`` commit/rollback and close reliably."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class User:
    id: int
    openid: str
    status: str
    pay_status: str = "unpaid"
    service_status: str = "unpaid"
    session_hash: str | None = None
    service_job_id: str | None = None
    balance_cent: int = 0
    remaining_uses: int = 0

    @property
    def storage_key(self) -> str:
        """Opaque, path-safe directory key derived from the WeChat identity."""
        digest = hashlib.sha256(self.openid.encode("utf-8")).hexdigest()[:20]
        return f"wechat-{self.id}-{digest}"


@dataclass(frozen=True)
class Admin:
    username: str


@dataclass(frozen=True)
class DatabaseConfig:
    driver: str
    sqlite_path: Path | None = None
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = "lookbook"
    charset: str = "utf8mb4"

    @classmethod
    def from_environment(cls, root: Path) -> "DatabaseConfig":
        # APP_DB_PATH remains as an explicit SQLite test/development override.
        legacy_path = os.environ.get("APP_DB_PATH")
        driver = os.environ.get("DB_DRIVER", "sqlite" if legacy_path else "mysql").lower()
        if driver == "sqlite":
            path = Path(legacy_path or os.environ.get("DB_PATH", root / "var/app.db")).resolve()
            return cls(driver="sqlite", sqlite_path=path)
        if driver != "mysql":
            raise RuntimeError("DB_DRIVER 仅支持 mysql 或 sqlite")
        required = {name: os.environ.get(name, "") for name in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")}
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"MySQL 配置缺失：{', '.join(missing)}")
        return cls(
            driver="mysql",
            host=required["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "3306")),
            user=required["DB_USER"],
            password=required["DB_PASSWORD"],
            database=required["DB_NAME"],
            charset=os.environ.get("DB_CHARSET", "utf8mb4"),
        )


class AuthStore:
    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config
        if config.sqlite_path is not None:
            config.sqlite_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.initialize()
        if config.sqlite_path is not None and config.sqlite_path.exists():
            config.sqlite_path.chmod(0o600)

    @property
    def placeholder(self) -> str:
        return "?" if self.config.driver == "sqlite" else "%s"

    def openid_match(self, left: str, right: str) -> str:
        if self.config.driver == "mysql":
            return f"BINARY {left} = BINARY {right}"
        return f"{left} = {right}"

    def connect(self):
        if self.config.driver == "sqlite":
            if self.config.sqlite_path is None:
                raise RuntimeError("SQLite 数据库路径未配置")
            connection = sqlite3.connect(self.config.sqlite_path, timeout=30, factory=ClosingSQLiteConnection)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - production dependency guard
            raise RuntimeError("使用 MySQL 需要安装 PyMySQL") from exc
        return pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=self.config.database,
            charset=self.config.charset,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

    def _execute(self, connection, sql: str, params: tuple[Any, ...] = ()):
        if self.config.driver == "sqlite":
            return connection.execute(sql, params)
        cursor = connection.cursor()
        cursor.execute(sql, params)
        return cursor

    def _legacy_user_schema(self, connection) -> bool:
        if self.config.driver == "sqlite":
            rows = connection.execute("PRAGMA table_info(users)").fetchall()
            columns = {str(row["name"]) for row in rows}
        else:
            cursor = connection.cursor()
            cursor.execute("SHOW TABLES LIKE 'users'")
            if cursor.fetchone() is None:
                return False
            cursor.execute("SHOW COLUMNS FROM users")
            columns = {str(row["Field"]) for row in cursor.fetchall()}
        return bool(columns) and ("openid" not in columns or "username" in columns or "password_hash" in columns)

    def initialize(self) -> None:
        with self.connect() as connection:
            if self.config.driver == "sqlite":
                # WAL lets progress/login readers continue while a credit or
                # session transaction is writing. SQLite remains a local/test
                # option; production uses MySQL.
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
            # Password-era rows cannot be mapped safely to a WeChat identity.
            # Remove that obsolete schema before creating the OpenID-only tables.
            if self._legacy_user_schema(connection):
                self._execute(connection, "DROP TABLE IF EXISTS sessions")
                self._execute(connection, "DROP TABLE IF EXISTS users")
            if self.config.driver == "sqlite":
                wx_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(wx_user)")}
                order_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(pay_order)")}
                if wx_columns and not {"openid", "pay_status", "create_time", "update_time", "status"} <= wx_columns:
                    connection.execute("DROP TABLE IF EXISTS pay_order")
                    connection.execute("DROP TABLE IF EXISTS wx_user")
                    order_columns = set()
                elif order_columns and not {"order_id", "openid", "total_fee", "pay_status", "create_time"} <= order_columns:
                    connection.execute("DROP TABLE IF EXISTS pay_order")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        openid TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                    CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
                    CREATE TABLE IF NOT EXISTS admin_sessions (
                        token_hash TEXT PRIMARY KEY,
                        username TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires ON admin_sessions(expires_at);
                    CREATE TABLE IF NOT EXISTS wx_user (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        openid TEXT NOT NULL UNIQUE,
                        pay_status INTEGER NOT NULL DEFAULT 0 CHECK(pay_status IN (0, 1)),
                        use_credits INTEGER NOT NULL DEFAULT 0 CHECK(use_credits >= 0),
                        balance_cent INTEGER NOT NULL DEFAULT 0 CHECK(balance_cent >= 0),
                        create_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        update_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status INTEGER NOT NULL DEFAULT 1 CHECK(status IN (0, 1))
                    );
                    CREATE TABLE IF NOT EXISTS pay_order (
                        order_id TEXT PRIMARY KEY,
                        openid TEXT NOT NULL,
                        total_fee INTEGER NOT NULL DEFAULT 1,
                        package_id TEXT,
                        package_name TEXT,
                        credits INTEGER,
                        pay_status INTEGER NOT NULL DEFAULT 0 CHECK(pay_status IN (0, 1)),
                        transaction_id TEXT UNIQUE,
                        job_id TEXT UNIQUE,
                        create_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        pay_time TEXT,
                        consumed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_pay_order_openid ON pay_order(openid, create_time);
                    CREATE TABLE IF NOT EXISTS generation_charge (
                        job_id TEXT PRIMARY KEY,
                        openid TEXT NOT NULL,
                        amount_cent INTEGER NOT NULL DEFAULT 0,
                        lot_order_id TEXT,
                        status TEXT NOT NULL CHECK(status IN ('reserved', 'completed', 'released')),
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_generation_charge_user
                        ON generation_charge(openid, created_at);
                    CREATE TABLE IF NOT EXISTS credit_lot (
                        order_id TEXT PRIMARY KEY,
                        openid TEXT NOT NULL,
                        package_id TEXT,
                        total_uses INTEGER NOT NULL CHECK(total_uses > 0),
                        remaining_uses INTEGER NOT NULL CHECK(remaining_uses >= 0),
                        amount_cent INTEGER NOT NULL CHECK(amount_cent >= 0),
                        remaining_amount_cent INTEGER NOT NULL CHECK(remaining_amount_cent >= 0),
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_credit_lot_user
                        ON credit_lot(openid, created_at);
                    """
                )
                wx_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(wx_user)")}
                if "use_credits" not in wx_columns:
                    connection.execute(
                        "ALTER TABLE wx_user ADD COLUMN use_credits INTEGER NOT NULL DEFAULT 0"
                    )
                    connection.execute("UPDATE wx_user SET use_credits = pay_status")
                if "balance_cent" not in wx_columns:
                    connection.execute("ALTER TABLE wx_user ADD COLUMN balance_cent INTEGER NOT NULL DEFAULT 0")
                    # Legacy pay_status represented a one-session entitlement,
                    # not purchased account balance; do not migrate it into a
                    # reusable credit.
                    connection.execute("UPDATE wx_user SET use_credits = 0, pay_status = 0")
                order_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(pay_order)")}
                if "job_id" not in order_columns:
                    connection.execute("ALTER TABLE pay_order ADD COLUMN job_id TEXT")
                    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_order_job ON pay_order(job_id)")
                if "consumed_at" not in order_columns:
                    connection.execute("ALTER TABLE pay_order ADD COLUMN consumed_at TEXT")
                if "session_token_hash" not in order_columns:
                    connection.execute("ALTER TABLE pay_order ADD COLUMN session_token_hash TEXT")
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_pay_order_session ON pay_order(session_token_hash, create_time)"
                    )
                if "order_status" not in order_columns:
                    connection.execute("ALTER TABLE pay_order ADD COLUMN order_status TEXT NOT NULL DEFAULT 'pending'")
                    connection.execute(
                        "UPDATE pay_order SET order_status = CASE "
                        "WHEN consumed_at IS NOT NULL OR job_id IS NOT NULL THEN 'consumed' "
                        "WHEN pay_status = 1 THEN 'paid' ELSE 'pending' END"
                    )
                for column, declaration in (
                    ("package_id", "TEXT"),
                    ("package_name", "TEXT"),
                    ("credits", "INTEGER"),
                ):
                    if column not in order_columns:
                        connection.execute(f"ALTER TABLE pay_order ADD COLUMN {column} {declaration}")
                charge_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(generation_charge)")}
                if "lot_order_id" not in charge_columns:
                    connection.execute("ALTER TABLE generation_charge ADD COLUMN lot_order_id TEXT")
                connection.execute(
                    "INSERT INTO credit_lot(order_id, openid, package_id, total_uses, remaining_uses, "
                    "amount_cent, remaining_amount_cent, created_at) "
                    "SELECT 'legacy-' || id, openid, 'legacy', use_credits, use_credits, balance_cent, "
                    "balance_cent, create_time FROM wx_user WHERE use_credits > 0 "
                    "AND NOT EXISTS (SELECT 1 FROM credit_lot WHERE credit_lot.openid = wx_user.openid)"
                )
            else:
                self._execute(connection, """
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        openid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL UNIQUE,
                        status ENUM('active', 'disabled') NOT NULL DEFAULT 'active',
                        created_at VARCHAR(40) NOT NULL,
                        updated_at VARCHAR(40) NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                self._execute(connection, """
                    CREATE TABLE IF NOT EXISTS sessions (
                        token_hash CHAR(64) NOT NULL PRIMARY KEY,
                        user_id BIGINT UNSIGNED NOT NULL,
                        created_at VARCHAR(40) NOT NULL,
                        expires_at VARCHAR(40) NOT NULL,
                        INDEX idx_sessions_user(user_id),
                        INDEX idx_sessions_expires(expires_at),
                        CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                self._execute(connection, """
                    CREATE TABLE IF NOT EXISTS admin_sessions (
                        token_hash CHAR(64) NOT NULL PRIMARY KEY,
                        username VARCHAR(64) NOT NULL,
                        created_at VARCHAR(40) NOT NULL,
                        expires_at VARCHAR(40) NOT NULL,
                        INDEX idx_admin_sessions_expires(expires_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                self._execute(connection, """
                    CREATE TABLE IF NOT EXISTS wx_user (
                        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        openid VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL UNIQUE COMMENT '微信唯一标识',
                        pay_status TINYINT NOT NULL DEFAULT 0 COMMENT '已停用兼容字段，固定为0',
                        use_credits INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '已停用兼容字段，固定为0',
                        balance_cent INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '可用服务余额，单位分',
                        create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        status TINYINT NOT NULL DEFAULT 1 COMMENT '账号正常/禁用'
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                cursor = connection.cursor()
                cursor.execute("SHOW COLUMNS FROM wx_user LIKE 'use_credits'")
                if cursor.fetchone() is None:
                    self._execute(
                        connection,
                        "ALTER TABLE wx_user ADD COLUMN use_credits INT UNSIGNED NOT NULL DEFAULT 0 "
                        "COMMENT '已支付且尚未使用的生成次数' AFTER pay_status",
                    )
                    self._execute(connection, "UPDATE wx_user SET use_credits = pay_status")
                cursor.execute("SHOW COLUMNS FROM wx_user LIKE 'balance_cent'")
                if cursor.fetchone() is None:
                    self._execute(connection, "ALTER TABLE wx_user ADD COLUMN balance_cent INT UNSIGNED NOT NULL DEFAULT 0 AFTER use_credits")
                self._execute(connection, """
                    CREATE TABLE IF NOT EXISTS pay_order (
                        order_id VARCHAR(32) NOT NULL PRIMARY KEY COMMENT '商户订单号',
                        openid VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                        total_fee INT NOT NULL DEFAULT 1 COMMENT '单位分',
                        package_id VARCHAR(32) NULL COMMENT '套餐ID快照',
                        package_name VARCHAR(64) NULL COMMENT '套餐名称快照',
                        credits INT UNSIGNED NULL COMMENT '购买生成次数快照',
                        pay_status TINYINT NOT NULL DEFAULT 0 COMMENT '0待支付 1已支付',
                        transaction_id VARCHAR(64) NULL UNIQUE,
                        job_id VARCHAR(32) NULL UNIQUE COMMENT '本次支付唯一绑定的生成任务',
                        create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        pay_time DATETIME NULL,
                        consumed_at DATETIME NULL,
                        session_token_hash CHAR(64) NULL COMMENT '创建订单的登录会话',
                        order_status ENUM('pending', 'paid', 'processing', 'consumed') NOT NULL DEFAULT 'pending',
                        INDEX idx_pay_order_openid(openid, create_time)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                cursor.execute("SHOW COLUMNS FROM pay_order LIKE 'job_id'")
                if cursor.fetchone() is None:
                    self._execute(connection, "ALTER TABLE pay_order ADD COLUMN job_id VARCHAR(32) NULL UNIQUE AFTER transaction_id")
                cursor.execute("SHOW COLUMNS FROM pay_order LIKE 'consumed_at'")
                if cursor.fetchone() is None:
                    self._execute(connection, "ALTER TABLE pay_order ADD COLUMN consumed_at DATETIME NULL AFTER pay_time")
                cursor.execute("SHOW COLUMNS FROM pay_order LIKE 'session_token_hash'")
                if cursor.fetchone() is None:
                    self._execute(connection, "ALTER TABLE pay_order ADD COLUMN session_token_hash CHAR(64) NULL AFTER openid")
                    self._execute(connection, "CREATE INDEX idx_pay_order_session ON pay_order(session_token_hash, create_time)")
                cursor.execute("SHOW COLUMNS FROM pay_order LIKE 'order_status'")
                order_status_column = cursor.fetchone()
                if order_status_column is None:
                    self._execute(
                        connection,
                        "ALTER TABLE pay_order ADD COLUMN order_status ENUM('pending', 'paid', 'processing', 'consumed') "
                        "NOT NULL DEFAULT 'pending' AFTER total_fee",
                    )
                    self._execute(
                        connection,
                        "UPDATE pay_order SET order_status = CASE "
                        "WHEN consumed_at IS NOT NULL OR job_id IS NOT NULL THEN 'consumed' "
                        "WHEN pay_status = 1 THEN 'paid' ELSE 'pending' END",
                    )
                elif "processing" not in str(order_status_column.get("Type", "")):
                    self._execute(
                        connection,
                        "ALTER TABLE pay_order MODIFY order_status "
                        "ENUM('pending', 'paid', 'processing', 'consumed') NOT NULL DEFAULT 'pending'",
                    )
                for column, definition in (
                    ("package_id", "VARCHAR(32) NULL COMMENT '套餐ID快照'"),
                    ("package_name", "VARCHAR(64) NULL COMMENT '套餐名称快照'"),
                    ("credits", "INT UNSIGNED NULL COMMENT '购买生成次数快照'"),
                ):
                    cursor.execute(f"SHOW COLUMNS FROM pay_order LIKE '{column}'")
                    if cursor.fetchone() is None:
                        self._execute(connection, f"ALTER TABLE pay_order ADD COLUMN {column} {definition}")
                self._execute(connection, """
                    CREATE TABLE IF NOT EXISTS generation_charge (
                        job_id VARCHAR(32) NOT NULL PRIMARY KEY,
                        openid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                        amount_cent INT UNSIGNED NOT NULL DEFAULT 0,
                        lot_order_id VARCHAR(32) NULL,
                        status ENUM('reserved', 'completed', 'released') NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        completed_at DATETIME NULL,
                        INDEX idx_generation_charge_user(openid, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                cursor.execute("SHOW COLUMNS FROM generation_charge LIKE 'lot_order_id'")
                if cursor.fetchone() is None:
                    self._execute(connection, "ALTER TABLE generation_charge ADD COLUMN lot_order_id VARCHAR(32) NULL AFTER amount_cent")
                self._execute(connection, """
                    CREATE TABLE IF NOT EXISTS credit_lot (
                        order_id VARCHAR(32) NOT NULL PRIMARY KEY,
                        openid VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                        package_id VARCHAR(32) NULL,
                        total_uses INT UNSIGNED NOT NULL,
                        remaining_uses INT UNSIGNED NOT NULL,
                        amount_cent INT UNSIGNED NOT NULL,
                        remaining_amount_cent INT UNSIGNED NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_credit_lot_user(openid, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                self._execute(connection, """
                    INSERT INTO credit_lot(order_id, openid, package_id, total_uses, remaining_uses,
                                           amount_cent, remaining_amount_cent, created_at)
                    SELECT CONCAT('legacy-', wx_user.id), wx_user.openid, 'legacy', wx_user.use_credits,
                           wx_user.use_credits, wx_user.balance_cent, wx_user.balance_cent, wx_user.create_time
                    FROM wx_user
                    WHERE wx_user.use_credits > 0
                      AND NOT EXISTS (SELECT 1 FROM credit_lot WHERE BINARY credit_lot.openid = BINARY wx_user.openid)
                """)
            marker = self.placeholder
            now = (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                   if self.config.driver == "mysql" else utc_now())
            openid_match = self.openid_match("wx_user.openid", "users.openid")
            self._execute(connection, f"""
                INSERT INTO wx_user(openid, pay_status, create_time, update_time, status)
                SELECT users.openid, 0, {marker}, {marker}, 1
                FROM users
                WHERE NOT EXISTS (SELECT 1 FROM wx_user WHERE {openid_match})
            """, (now, now))
            connection.commit()

    def _ensure_payment_profile(self, connection, user_id: int, openid: str) -> None:
        marker = self.placeholder
        row = self._execute(
            connection, f"SELECT id FROM wx_user WHERE openid = {marker}", (openid,)
        ).fetchone()
        if row is None:
            now = (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                   if self.config.driver == "mysql" else utc_now())
            self._execute(
                connection,
                f"INSERT INTO wx_user(openid, pay_status, create_time, update_time, status) "
                f"VALUES ({marker}, 0, {marker}, {marker}, 1)",
                (openid, now, now),
            )

    @staticmethod
    def _validate_openid(openid: str) -> str:
        value = openid.strip()
        if not value or len(value) > 128 or any(ord(char) < 33 for char in value):
            raise HTTPException(status_code=401, detail="微信身份标识无效")
        return value

    def find_or_create_user(self, openid: str) -> User:
        openid = self._validate_openid(openid)
        now = utc_now()
        marker = self.placeholder
        with self.connect() as connection:
            row = self._execute(
                connection, f"SELECT id, openid, status FROM users WHERE openid = {marker}", (openid,)
            ).fetchone()
            if row is None:
                try:
                    self._execute(
                        connection,
                        f"INSERT INTO users(openid, status, created_at, updated_at) VALUES ({marker}, {marker}, {marker}, {marker})",
                        (openid, "active", now, now),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                row = self._execute(
                    connection, f"SELECT id, openid, status FROM users WHERE openid = {marker}", (openid,)
                ).fetchone()
            if row is None:
                raise HTTPException(status_code=500, detail="创建微信用户失败")
            if str(row["status"]) != "active":
                raise HTTPException(status_code=403, detail="当前用户已被停用")
            self._ensure_payment_profile(connection, int(row["id"]), str(row["openid"]))
            self._execute(connection, f"UPDATE users SET updated_at = {marker} WHERE id = {marker}", (now, row["id"]))
            connection.commit()
        # Balance is resolved from wx_user whenever the session is read; the
        # returned creation-time user object intentionally has no stale credit.
        return User(int(row["id"]), str(row["openid"]), str(row["status"]))

    def get_user(self, user_id: int) -> User | None:
        marker = self.placeholder
        with self.connect() as connection:
            row = self._execute(
                connection, f"""
                    SELECT id, openid, status FROM users WHERE id = {marker}
                """, (user_id,)
            ).fetchone()
        return None if row is None else User(
            int(row["id"]), str(row["openid"]), str(row["status"]),
        )

    def consume_service_order(self, user: User, job_id: str) -> bool:
        """Compatibility alias: reserve one account-level generation credit."""
        return self.reserve_generation(user.openid, job_id)

    def reserve_generation(self, openid: str, job_id: str) -> bool:
        """Reserve one use without charging it until generation succeeds."""
        marker = self.placeholder
        with self.connect() as connection:
            if self.config.driver == "sqlite":
                connection.execute("BEGIN IMMEDIATE")
            suffix = " FOR UPDATE" if self.config.driver == "mysql" else ""
            account = self._execute(
                connection,
                f"SELECT use_credits FROM wx_user WHERE openid = {marker}{suffix}",
                (openid,),
            ).fetchone()
            reserved = self._execute(
                connection,
                f"SELECT COUNT(*) AS count FROM generation_charge WHERE openid = {marker} AND status = 'reserved'",
                (openid,),
            ).fetchone()
            available = int(account["use_credits"]) - int(reserved["count"]) if account else 0
            if available < 1:
                connection.rollback()
                return False
            try:
                self._execute(
                    connection,
                    f"INSERT INTO generation_charge(job_id, openid, amount_cent, status, created_at) "
                    f"VALUES ({marker}, {marker}, 0, 'reserved', {marker})",
                    (job_id, openid, utc_now()),
                )
            except Exception:
                connection.rollback()
                return False
            connection.commit()
        return True

    def resume_generation(self, openid: str, job_id: str) -> bool:
        """Restore the original job reservation without creating a second charge."""
        marker = self.placeholder
        with self.connect() as connection:
            if self.config.driver == "sqlite":
                connection.execute("BEGIN IMMEDIATE")
            suffix = " FOR UPDATE" if self.config.driver == "mysql" else ""
            charge = self._execute(
                connection,
                f"SELECT openid, status FROM generation_charge WHERE job_id = {marker}{suffix}",
                (job_id,),
            ).fetchone()
            if charge is None or str(charge["openid"]) != openid or str(charge["status"]) == "completed":
                connection.rollback()
                return False
            if str(charge["status"]) == "reserved":
                connection.commit()
                return True

            account = self._execute(
                connection, f"SELECT use_credits FROM wx_user WHERE openid = {marker}{suffix}", (openid,)
            ).fetchone()
            reserved = self._execute(
                connection,
                f"SELECT COUNT(*) AS count FROM generation_charge WHERE openid = {marker} AND status = 'reserved'",
                (openid,),
            ).fetchone()
            available = int(account["use_credits"]) - int(reserved["count"]) if account else 0
            if available < 1:
                connection.rollback()
                return False
            updated = self._execute(
                connection,
                f"UPDATE generation_charge SET status = 'reserved', completed_at = NULL "
                f"WHERE job_id = {marker} AND openid = {marker} AND status = 'released'",
                (job_id, openid),
            ).rowcount == 1
            if not updated:
                connection.rollback()
                return False
            connection.commit()
        return True

    def mark_service_order_consumed(self, job_id: str) -> bool:
        return self.finalize_generation(job_id, success=True)

    def finalize_generation(self, job_id: str, *, success: bool) -> bool:
        """Charge a successful reservation once, or release a failed one."""
        marker = self.placeholder
        now = (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
               if self.config.driver == "mysql" else utc_now())
        with self.connect() as connection:
            if self.config.driver == "sqlite":
                connection.execute("BEGIN IMMEDIATE")
            suffix = " FOR UPDATE" if self.config.driver == "mysql" else ""
            charge = self._execute(
                connection,
                f"SELECT openid, status FROM generation_charge WHERE job_id = {marker}{suffix}",
                (job_id,),
            ).fetchone()
            wanted_status = "completed" if success else "released"
            if charge is not None and str(charge["status"]) == wanted_status:
                connection.commit()
                return True
            if charge is None or str(charge["status"]) != "reserved":
                connection.rollback()
                return False
            if success:
                account = self._execute(
                    connection,
                    f"SELECT balance_cent, use_credits FROM wx_user WHERE openid = {marker}{suffix}",
                    (charge["openid"],),
                ).fetchone()
                if account is None or int(account["use_credits"]) < 1:
                    connection.rollback()
                    return False
                lot = self._execute(
                    connection,
                    f"SELECT order_id, remaining_uses, remaining_amount_cent FROM credit_lot "
                    f"WHERE openid = {marker} AND remaining_uses > 0 ORDER BY created_at, order_id LIMIT 1{suffix}",
                    (charge["openid"],),
                ).fetchone()
                if lot is None:
                    # Compatibility repair for balances written by an older
                    # deployment or an administrative import without lot rows.
                    repair_order_id = "legacy-" + hashlib.sha256(str(charge["openid"]).encode("utf-8")).hexdigest()[:24]
                    try:
                        self._execute(
                            connection,
                            f"INSERT INTO credit_lot(order_id, openid, package_id, total_uses, remaining_uses, "
                            f"amount_cent, remaining_amount_cent, created_at) VALUES "
                            f"({marker}, {marker}, 'legacy', {marker}, {marker}, {marker}, {marker}, {marker})",
                            (repair_order_id, charge["openid"], account["use_credits"], account["use_credits"],
                             account["balance_cent"], account["balance_cent"], now),
                        )
                    except Exception:
                        connection.rollback()
                        return False
                    lot = self._execute(
                        connection,
                        f"SELECT order_id, remaining_uses, remaining_amount_cent FROM credit_lot "
                        f"WHERE order_id = {marker}{suffix}",
                        (repair_order_id,),
                    ).fetchone()
                    if lot is None:
                        connection.rollback()
                        return False
                uses_before = int(lot["remaining_uses"])
                amount_before = int(lot["remaining_amount_cent"])
                amount_to_debit = amount_before // uses_before
                lot_updated = self._execute(
                    connection,
                    f"UPDATE credit_lot SET remaining_uses = remaining_uses - 1, "
                    f"remaining_amount_cent = remaining_amount_cent - {marker} "
                    f"WHERE order_id = {marker} AND remaining_uses > 0 AND remaining_amount_cent >= {marker}",
                    (amount_to_debit, lot["order_id"], amount_to_debit),
                ).rowcount == 1
                if not lot_updated:
                    connection.rollback()
                    return False
                credits_after = int(account["use_credits"]) - 1
                balance_after = int(account["balance_cent"]) - amount_to_debit
                debited = self._execute(
                    connection,
                    f"UPDATE wx_user SET use_credits = {marker}, balance_cent = {marker}, "
                    f"pay_status = {marker}, update_time = {marker} "
                    f"WHERE openid = {marker} AND use_credits >= 1 AND balance_cent >= {marker}",
                    (credits_after, balance_after, 1 if credits_after > 0 else 0,
                     now, charge["openid"], amount_to_debit),
                ).rowcount == 1
                if not debited:
                    connection.rollback()
                    return False
            status = "completed" if success else "released"
            if success:
                updated = self._execute(
                    connection,
                    f"UPDATE generation_charge SET status = {marker}, completed_at = {marker}, "
                    f"amount_cent = {marker}, lot_order_id = {marker} "
                    f"WHERE job_id = {marker} AND status = 'reserved'",
                    (status, now, amount_to_debit, lot["order_id"], job_id),
                ).rowcount == 1
            else:
                updated = self._execute(
                    connection,
                    f"UPDATE generation_charge SET status = {marker}, completed_at = {marker} "
                    f"WHERE job_id = {marker} AND status = 'reserved'",
                    (status, now, job_id),
                ).rowcount == 1
            if not updated:
                connection.rollback()
                return False
            connection.commit()
        return True

    def account_summary(self, openid: str) -> dict[str, Any]:
        marker = self.placeholder
        with self.connect() as connection:
            account = self._execute(connection, f"SELECT balance_cent, use_credits, create_time FROM wx_user WHERE openid = {marker}", (openid,)).fetchone()
            reserved = self._execute(connection, f"SELECT COUNT(*) AS count FROM generation_charge WHERE openid = {marker} AND status = 'reserved'", (openid,)).fetchone()
            orders = self._execute(
                connection,
                f"SELECT order_id, total_fee, package_id, package_name, credits, pay_status, order_status, create_time, pay_time "
                f"FROM pay_order WHERE openid = {marker} AND pay_status = 1 AND order_status = 'paid' "
                "ORDER BY pay_time DESC, create_time DESC LIMIT 100",
                (openid,),
            ).fetchall()
            charges = self._execute(connection, f"SELECT job_id, amount_cent, status, created_at, completed_at FROM generation_charge WHERE openid = {marker} ORDER BY created_at DESC LIMIT 100", (openid,)).fetchall()
        credits = int(account["use_credits"]) if account else 0
        held = int(reserved["count"]) if reserved else 0
        return {
            "balance_cent": int(account["balance_cent"]) if account else 0,
            "remaining_uses": max(0, credits - held),
            "reserved_uses": held,
            "recharge_records": [dict(row) for row in orders],
            "consumption_records": [dict(row) for row in charges if str(row["status"]) == "completed"],
        }

    def reserved_generations(self) -> list[dict[str, Any]]:
        """Return reservations for the job watchdog and reconciliation pass."""
        with self.connect() as connection:
            rows = self._execute(
                connection,
                "SELECT job_id, openid, created_at FROM generation_charge "
                "WHERE status = 'reserved' ORDER BY created_at",
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(48)
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
        marker = self.placeholder
        with self.connect() as connection:
            self._execute(connection, f"DELETE FROM sessions WHERE expires_at < {marker}", (now,))
            self._execute(
                connection,
                f"INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES ({marker}, {marker}, {marker}, {marker})",
                (self._hash_token(token), user_id, now, expires_at),
            )
            connection.commit()
        return token

    def create_admin_session(self, username: str) -> str:
        token = secrets.token_urlsafe(48)
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=ADMIN_SESSION_HOURS)).isoformat()
        marker = self.placeholder
        with self.connect() as connection:
            self._execute(connection, f"DELETE FROM admin_sessions WHERE expires_at < {marker}", (now,))
            self._execute(
                connection,
                f"INSERT INTO admin_sessions(token_hash, username, created_at, expires_at) "
                f"VALUES ({marker}, {marker}, {marker}, {marker})",
                (self._hash_token(token), username, now, expires_at),
            )
            connection.commit()
        return token

    def admin_for_token(
        self, token: str | None, expected_principal: str, display_username: str | None = None,
    ) -> Admin | None:
        if not token:
            return None
        marker = self.placeholder
        with self.connect() as connection:
            row = self._execute(
                connection,
                f"SELECT username FROM admin_sessions WHERE token_hash = {marker} "
                f"AND expires_at > {marker}",
                (self._hash_token(token), utc_now()),
            ).fetchone()
        if row is None or not hmac.compare_digest(
            str(row["username"]).encode("utf-8"), expected_principal.encode("utf-8")
        ):
            return None
        return Admin(display_username or str(row["username"]))

    def delete_admin_session(self, token: str | None) -> None:
        if not token:
            return
        marker = self.placeholder
        with self.connect() as connection:
            self._execute(
                connection,
                f"DELETE FROM admin_sessions WHERE token_hash = {marker}",
                (self._hash_token(token),),
            )
            connection.commit()

    def admin_users(self, page: int, page_size: int, search: str = "") -> tuple[list[dict[str, Any]], int]:
        marker = self.placeholder
        where = ""
        params: tuple[Any, ...] = ()
        if search:
            where = f"WHERE users.openid LIKE {marker}"
            params = (f"%{search}%",)
        with self.connect() as connection:
            total_row = self._execute(
                connection, f"SELECT COUNT(*) AS count FROM users {where}", params
            ).fetchone()
            rows = self._execute(
                connection,
                f"""
                SELECT users.id, users.openid, users.status, users.created_at,
                       users.updated_at AS last_login_at,
                       COALESCE(wx_user.balance_cent, 0) AS balance_cent,
                       COALESCE(wx_user.use_credits, 0) AS use_credits,
                       COALESCE(reserved.reserved_uses, 0) AS reserved_uses
                FROM users
                LEFT JOIN wx_user ON wx_user.openid = users.openid
                LEFT JOIN (
                    SELECT openid, COUNT(*) AS reserved_uses
                    FROM generation_charge WHERE status = 'reserved' GROUP BY openid
                ) reserved ON reserved.openid = users.openid
                {where}
                ORDER BY users.created_at DESC, users.id DESC
                LIMIT {marker} OFFSET {marker}
                """,
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["remaining_uses"] = max(0, int(item.pop("use_credits")) - int(item.pop("reserved_uses")))
            item["balance_cent"] = int(item["balance_cent"])
            items.append(item)
        return items, int(total_row["count"])

    def admin_user(self, user_id: int) -> dict[str, Any] | None:
        marker = self.placeholder
        with self.connect() as connection:
            row = self._execute(
                connection,
                f"""
                SELECT users.id, users.openid, users.status, users.created_at,
                       users.updated_at AS last_login_at,
                       COALESCE(wx_user.balance_cent, 0) AS balance_cent,
                       COALESCE(wx_user.use_credits, 0) AS use_credits,
                       (SELECT COUNT(*) FROM generation_charge
                        WHERE generation_charge.openid = users.openid
                          AND generation_charge.status = 'reserved') AS reserved_uses
                FROM users LEFT JOIN wx_user ON wx_user.openid = users.openid
                WHERE users.id = {marker}
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["remaining_uses"] = max(0, int(item.pop("use_credits")) - int(item.pop("reserved_uses")))
        item["balance_cent"] = int(item["balance_cent"])
        return item

    def admin_payments(
        self, page: int, page_size: int, user_id: int | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        marker = self.placeholder
        where = ""
        params: tuple[Any, ...] = ()
        if user_id is not None:
            where = f"WHERE users.id = {marker}"
            params = (user_id,)
        with self.connect() as connection:
            total_row = self._execute(
                connection,
                f"SELECT COUNT(*) AS count FROM pay_order LEFT JOIN users "
                f"ON users.openid = pay_order.openid {where}",
                params,
            ).fetchone()
            rows = self._execute(
                connection,
                f"""
                SELECT pay_order.order_id, pay_order.openid, users.id AS user_id,
                       pay_order.package_id, pay_order.package_name, pay_order.credits,
                       pay_order.total_fee, pay_order.pay_status, pay_order.order_status,
                       pay_order.create_time, pay_order.pay_time, pay_order.transaction_id
                FROM pay_order LEFT JOIN users ON users.openid = pay_order.openid
                {where}
                ORDER BY pay_order.create_time DESC, pay_order.order_id DESC
                LIMIT {marker} OFFSET {marker}
                """,
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return [dict(row) for row in rows], int(total_row["count"])

    def user_for_token(self, token: str | None) -> User | None:
        if not token:
            return None
        marker = self.placeholder
        token_hash = self._hash_token(token)
        with self.connect() as connection:
            row = self._execute(connection, f"""
                SELECT users.id, users.openid, users.status
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = {marker} AND sessions.expires_at > {marker}
                  AND users.status = 'active'
            """, (token_hash, utc_now())).fetchone()
            account = None if row is None else self._execute(connection, f"SELECT balance_cent, use_credits FROM wx_user WHERE openid = {marker}", (row["openid"],)).fetchone()
            reserved = None if row is None else self._execute(
                connection,
                f"SELECT COUNT(*) AS count FROM generation_charge WHERE openid = {marker} AND status = 'reserved'",
                (row["openid"],),
            ).fetchone()
        if row is None:
            return None
        balance = int(account["balance_cent"]) if account else 0
        held = int(reserved["count"]) if reserved else 0
        credits = max(0, int(account["use_credits"]) - held) if account else 0
        service_status = "paid" if credits > 0 else "unpaid"
        return User(
            int(row["id"]), str(row["openid"]), str(row["status"]),
            "paid" if service_status == "paid" else "unpaid",
            service_status, token_hash, None, balance, credits,
        )

    # Compatibility alias retained for callers outside this repository.
    def consume_use_credit(self, openid: str, job_id: str | None = None) -> bool:
        return False

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        marker = self.placeholder
        with self.connect() as connection:
            self._execute(connection, f"DELETE FROM sessions WHERE token_hash = {marker}", (self._hash_token(token),))
            connection.commit()

    def delete_session_hash(self, token_hash: str | None) -> None:
        if not token_hash:
            return
        marker = self.placeholder
        with self.connect() as connection:
            self._execute(connection, f"DELETE FROM sessions WHERE token_hash = {marker}", (token_hash,))
            connection.commit()

    def clear_sessions(self) -> None:
        with self.connect() as connection:
            self._execute(connection, "DELETE FROM sessions")
            connection.commit()


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    # Remove the cookie path used by older subdirectory deployments. Keeping
    # two cookies with the same name can make PHP and FastAPI read different
    # session tokens on /jewelry-lookbook-sheet/* requests.
    response.delete_cookie(SESSION_COOKIE, path="/jewelry-lookbook-sheet")
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400,
        httponly=True, secure=secure, samesite="lax", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE, path="/jewelry-lookbook-sheet")


def set_admin_session_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        ADMIN_SESSION_COOKIE, token, max_age=ADMIN_SESSION_HOURS * 3600,
        httponly=True, secure=secure, samesite="strict", path="/admin",
    )


def clear_admin_session_cookie(response: Response) -> None:
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/admin")


def set_oauth_state_cookie(response: Response, state: str, secure: bool) -> None:
    response.set_cookie(
        OAUTH_STATE_COOKIE, state, max_age=600,
        httponly=True, secure=secure, samesite="lax", path="/api/auth/wechat/callback",
    )


def clear_oauth_state_cookie(response: Response) -> None:
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/wechat/callback")


def valid_oauth_state(request: Request, received: str) -> bool:
    expected = request.cookies.get(OAUTH_STATE_COOKIE, "")
    return bool(expected and received and hmac.compare_digest(expected, received))


def current_user(request: Request, store: AuthStore) -> User:
    user = store.user_for_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="请先使用微信登录")
    return user
