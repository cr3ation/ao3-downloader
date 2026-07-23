"""Offline account recovery: python -m app.adminctl <command>

Run it inside the container as the app user, not root:

    docker compose exec --user appuser ao3-downloader python -m app.adminctl list

`docker compose exec` bypasses the entrypoint and would otherwise run as root,
leaving root-owned app.db-wal/-shm files that the app itself can no longer write.
"""
import sys
from datetime import datetime, timezone

from . import db
from .auth import hash_password, password_problem
from .config import Settings

USAGE = """Usage: python -m app.adminctl <command>

  list                            Show every account
  reset-password <user> <pass>    Set a local password (also clears sessions)
  promote <user>                  Grant the admin role
  demote <user>                   Remove the admin role (never the last admin)
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    db_path = Settings.from_env().db_path
    if not db_path.exists():
        print(f"No database at {db_path}. Start the app once to create it.")
        return 1

    command, args = argv[0], argv[1:]
    now = datetime.now(timezone.utc).isoformat()

    if command == "list":
        users = db.list_users(db_path)
        if not users:
            print("No accounts.")
            return 0
        print(f"{'USERNAME':24} {'ROLE':8} {'PROVIDER':10} CREATED")
        for u in users:
            print(f"{u.username:24} {u.role:8} {u.provider:10} {u.created_at[:10]}")
        return 0

    if command == "reset-password":
        if len(args) != 2:
            print("Usage: reset-password <username> <new-password>")
            return 1
        username, password = args
        problem = password_problem(password)
        if problem:
            print(problem)
            return 1
        user = db.get_user_by_username(db_path, username)
        if user is None:
            print(f"No such account: {username}")
            return 1
        db.set_password_hash(db_path, user.id, hash_password(password))
        db.delete_sessions_for_user(db_path, user.id)
        print(f"Password set for '{user.username}'. Existing sessions were revoked.")
        return 0

    if command in ("promote", "demote"):
        if len(args) != 1:
            print(f"Usage: {command} <username>")
            return 1
        user = db.get_user_by_username(db_path, args[0])
        if user is None:
            print(f"No such account: {args[0]}")
            return 1
        if command == "demote" and db.count_admins(db_path, excluding_id=user.id) == 0:
            print("Refusing: this is the last administrator.")
            return 1
        db.set_role(db_path, user.id, "admin" if command == "promote" else "user")
        db.delete_sessions_for_user(db_path, user.id)
        print(f"'{user.username}' is now {'an admin' if command == 'promote' else 'a regular user'}.")
        return 0

    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
