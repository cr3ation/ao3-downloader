"""SQLite data layer: schema, users, sessions, settings and OIDC login state.

Free functions taking a db_path, mirroring the metadata helpers in downloader.py.
No shared connection: each call opens the file, which costs tens of microseconds
and removes every thread-affinity question. The tables hold single-digit row
counts and every query is an indexed point lookup.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import OidcState, Session, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT,
    role          TEXT    NOT NULL DEFAULT 'user',
    provider      TEXT    NOT NULL DEFAULT 'local',
    subject       TEXT,
    created_at    TEXT    NOT NULL,
    last_login_at TEXT
);

-- Partial: most rows have subject NULL and those must not collide.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_subject
    ON users(subject) WHERE subject IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT    PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token   TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    last_used_at TEXT    NOT NULL,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oidc_states (
    state         TEXT PRIMARY KEY,
    nonce         TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    redirect_uri  TEXT NOT NULL,
    next_path     TEXT,
    created_at    TEXT NOT NULL
);
"""


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        # WAL needs shared memory, which is unavailable on NFS/SMB shares — the
        # config volume may well be one, so fall back rather than fail.
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            print(f"[db] WAL unavailable on this filesystem (journal_mode={mode}); using the default journal.")
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- users


def _to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        provider=row["provider"],
        subject=row["subject"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


def create_user(
    db_path: Path,
    username: str,
    *,
    now: str,
    password_hash: str | None = None,
    role: str = "user",
    provider: str = "local",
    subject: str | None = None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, provider, subject, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (username, password_hash, role, provider, subject, now),
        )
        return int(cur.lastrowid)


def get_user(db_path: Path, user_id: int) -> User | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _to_user(row) if row else None


def get_user_by_username(db_path: Path, username: str) -> User | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _to_user(row) if row else None


def get_user_by_subject(db_path: Path, subject: str) -> User | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE subject = ?", (subject,)).fetchone()
    return _to_user(row) if row else None


def list_users(db_path: Path) -> list[User]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return [_to_user(r) for r in rows]


def count_users(db_path: Path) -> int:
    with _connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def count_admins(db_path: Path, *, excluding_id: int | None = None) -> int:
    """Admins that would remain if `excluding_id` were removed or demoted.

    Lives here so every caller — the delete route, the role route and adminctl —
    shares one definition of "the last administrator".
    """
    sql = "SELECT COUNT(*) FROM users WHERE role = 'admin'"
    params: tuple = ()
    if excluding_id is not None:
        sql += " AND id != ?"
        params = (excluding_id,)
    with _connect(db_path) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def set_password_hash(db_path: Path, user_id: int, password_hash: str | None) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def set_role(db_path: Path, user_id: int, role: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def link_subject(db_path: Path, user_id: int, subject: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE users SET subject = ? WHERE id = ?", (subject, user_id))


def touch_login(db_path: Path, user_id: int, now: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))


def delete_user(db_path: Path, user_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ---------------------------------------------------------------- sessions


def create_session(
    db_path: Path,
    *,
    token_hash: str,
    user_id: int,
    csrf_token: str,
    created_at: str,
    expires_at: str,
    user_agent: str | None,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at,"
            " last_used_at, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_hash, user_id, csrf_token, created_at, expires_at, created_at, user_agent),
        )


def get_session(db_path: Path, token_hash: str) -> Session | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
    if not row:
        return None
    return Session(
        token_hash=row["token_hash"],
        user_id=row["user_id"],
        csrf_token=row["csrf_token"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_used_at=row["last_used_at"],
        user_agent=row["user_agent"],
    )


def touch_session(db_path: Path, token_hash: str, now: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE sessions SET last_used_at = ? WHERE token_hash = ?", (now, token_hash))


def delete_session(db_path: Path, token_hash: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_sessions_for_user(db_path: Path, user_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired_sessions(db_path: Path, now: str) -> int:
    with _connect(db_path) as conn:
        return conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount


# ---------------------------------------------------------------- settings


def get_settings(db_path: Path, prefix: str = "") -> dict[str, str]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?", (f"{prefix}%",)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_settings(db_path: Path, values: dict[str, str], now: str) -> None:
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [(k, v, now) for k, v in values.items()],
        )


# ---------------------------------------------------------------- OIDC state


def create_oidc_state(
    db_path: Path,
    *,
    state: str,
    nonce: str,
    code_verifier: str,
    redirect_uri: str,
    next_path: str,
    created_at: str,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO oidc_states (state, nonce, code_verifier, redirect_uri, next_path, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (state, nonce, code_verifier, redirect_uri, next_path, created_at),
        )


def consume_oidc_state(db_path: Path, state: str) -> OidcState | None:
    """Fetch and delete in one transaction, so a replayed callback finds nothing."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM oidc_states WHERE state = ?", (state,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM oidc_states WHERE state = ?", (state,))
    return OidcState(
        state=row["state"],
        nonce=row["nonce"],
        code_verifier=row["code_verifier"],
        redirect_uri=row["redirect_uri"],
        next_path=row["next_path"] or "/",
        created_at=row["created_at"],
    )


def purge_oidc_states(db_path: Path, cutoff: str) -> int:
    with _connect(db_path) as conn:
        return conn.execute("DELETE FROM oidc_states WHERE created_at <= ?", (cutoff,)).rowcount
