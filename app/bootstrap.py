"""First-boot account seeding and the break-glass password reset.

The admin credentials are read straight from the environment here and dropped
immediately — deliberately never stored on Settings, so a plaintext password
cannot surface in a traceback or a repr of the settings object.
"""
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .auth import hash_password


def _banner(lines: list[str]) -> None:
    width = max(len(line) for line in lines) + 4
    print("=" * width)
    for line in lines:
        print(f"  {line}")
    print("=" * width, flush=True)


def seed_admin(db_path: Path) -> None:
    """Create the first administrator when the users table is empty."""
    if db.count_users(db_path) > 0:
        return

    username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    password = os.getenv("ADMIN_PASSWORD", "")
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True

    now = datetime.now(timezone.utc).isoformat()
    db.create_user(db_path, username, now=now, password_hash=hash_password(password), role="admin")

    if generated:
        _banner(
            [
                "AO3 Downloader — first run: administrator account created",
                "",
                f"  username: {username}",
                f"  password: {password}",
                "",
                "This password is shown once and is not stored anywhere.",
                "Set ADMIN_PASSWORD in .env to choose your own next time.",
            ]
        )
    else:
        print(f"[bootstrap] Created administrator '{username}' from ADMIN_PASSWORD.", flush=True)


def apply_password_reset(db_path: Path) -> None:
    """Break-glass reset via ADMIN_PASSWORD_RESET, for a forgotten password.

    Recovery becomes "edit .env and restart" with no shell access, which is the
    right ergonomics on a NAS.
    """
    password = os.getenv("ADMIN_PASSWORD_RESET", "")
    if not password:
        return

    username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    now = datetime.now(timezone.utc).isoformat()
    user = db.get_user_by_username(db_path, username)

    if user is None:
        db.create_user(db_path, username, now=now, password_hash=hash_password(password), role="admin")
        action = "created"
    else:
        db.set_password_hash(db_path, user.id, hash_password(password))
        db.set_role(db_path, user.id, "admin")
        db.delete_sessions_for_user(db_path, user.id)
        action = "reset"

    _banner(
        [
            f"ADMIN_PASSWORD_RESET applied: account '{username}' {action} and promoted to admin.",
            "All of its sessions were revoked.",
            "",
            "Remove ADMIN_PASSWORD_RESET from .env and restart — it re-applies on every boot.",
        ]
    )
