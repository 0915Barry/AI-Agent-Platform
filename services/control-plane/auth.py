#!/usr/bin/env python3
"""M16 本地账户与短期会话存储。

密码使用独立随机盐和 PBKDF2-SHA256；浏览器只持有随机会话令牌，数据库只保存
令牌的 SHA-256 摘要。该模块不提供公网身份系统，控制面仍必须绑定 loopback。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 12 * 60 * 60


class AuthError(RuntimeError):
    """账户输入、凭据或会话无效。"""


class AuthStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.initialize()
        # 该数据库包含密码派生值与有效会话摘要，只允许控制面宿主账户读取。
        self.database_path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash BLOB PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at)"
            )

    @staticmethod
    def public_user(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "username": row["username"]}

    @staticmethod
    def validate_username(username: str) -> str:
        username = username.strip().lower()
        if not USERNAME_RE.fullmatch(username):
            raise AuthError("用户名需为 3-32 位小写字母、数字、点、下划线或连字符")
        return username

    @staticmethod
    def validate_password(password: str) -> None:
        password_bytes = password.encode("utf-8")
        if len(password_bytes) < 12 or len(password_bytes) > 256:
            raise AuthError("密码长度必须为 12-256 个 UTF-8 字节")

    def create_user(self, username: str, password: str, *, claim_unowned: bool = True) -> dict[str, Any]:
        username = self.validate_username(username)
        self.validate_password(password)
        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        user_id = f"user-{uuid.uuid4().hex[:16]}"
        now = int(time.time())
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO users(id,username,password_salt,password_hash,password_iterations,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (user_id, username, salt, password_hash, PBKDF2_ITERATIONS, now),
                )
                # 首次启用 M16 时保留旧实例：尚无 owner 的记录归给首个创建的用户。
                if claim_unowned:
                    has_instances = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instances'"
                    ).fetchone()
                    if has_instances is not None:
                        columns = {
                            row["name"]
                            for row in connection.execute("PRAGMA table_info(instances)").fetchall()
                        }
                        if "owner_id" not in columns:
                            connection.execute("ALTER TABLE instances ADD COLUMN owner_id TEXT")
                        connection.execute(
                            "UPDATE instances SET owner_id=? WHERE owner_id IS NULL", (user_id,)
                        )
        except sqlite3.IntegrityError as error:
            raise AuthError(f"用户已存在：{username}") from error
        return {"id": user_id, "username": username}

    def has_users(self) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        return row is not None

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        username = username.strip().lower()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            # 对不存在的用户也执行一次同量级哈希，减少用户名枚举的时间差。
            hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"\0" * 16, PBKDF2_ITERATIONS)
            return None
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), row["password_salt"], row["password_iterations"]
        )
        return self.public_user(row) if hmac.compare_digest(candidate, row["password_hash"]) else None

    @staticmethod
    def token_digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("ascii", errors="ignore")).digest()

    def create_session(self, user_id: str) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + SESSION_TTL_SECONDS
        with self.connect() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            connection.execute(
                "INSERT INTO auth_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (self.token_digest(token), user_id, now, expires_at),
            )
        return token, expires_at

    def session_user(self, token: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                "SELECT u.id,u.username FROM auth_sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>?",
                (self.token_digest(token), now),
            ).fetchone()
        return None if row is None else self.public_user(row)

    def revoke_session(self, token: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?", (self.token_digest(token),)
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-user")
    create.add_argument("--username", required=True)
    create.add_argument("--claim-unowned", action="store_true")
    args = parser.parse_args()
    password = getpass.getpass("新用户密码：")
    confirmation = getpass.getpass("再次输入密码：")
    if password != confirmation:
        raise SystemExit("两次输入的密码不一致")
    try:
        user = AuthStore(args.database).create_user(
            args.username, password, claim_unowned=args.claim_unowned
        )
    except AuthError as error:
        raise SystemExit(str(error)) from error
    print(f"AUTH_USER_READY username={user['username']} id={user['id']}")


if __name__ == "__main__":
    main()
