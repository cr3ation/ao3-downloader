#!/bin/sh
# Runs as root: adopt the requested identity and timezone, then drop privileges.
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}
TZ=${TZ:-Europe/Stockholm}

if [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
else
    echo "[entrypoint] Unknown timezone '$TZ' — keeping UTC." >&2
fi

# Already running as a non-root user (compose `user:` directive): nothing to do.
if [ "$(id -u)" != "0" ]; then
    echo "[entrypoint] Running as uid $(id -u) — skipping PUID/PGID setup."
    exec "$@"
fi

# Reuse an existing group with that GID (e.g. 100 = "users" on most NAS boxes)
# rather than renumbering ours into a duplicate.
if getent group "$PGID" >/dev/null 2>&1; then
    RUN_GROUP=$(getent group "$PGID" | cut -d: -f1)
else
    groupmod -o -g "$PGID" appuser
    RUN_GROUP=appuser
fi
usermod -o -u "$PUID" -g "$RUN_GROUP" appuser

# config is ours; downloads may hold thousands of files shared with Calibre,
# so only the directory itself is adjusted, never its contents.
chown -R "$PUID:$PGID" /app/config 2>/dev/null || true
chown "$PUID:$PGID" /app/downloads 2>/dev/null || true

# Fail loudly here rather than with an opaque traceback from uvicorn.
if ! gosu appuser test -r /app/app/main.py; then
    echo "[entrypoint] ERROR: uid $PUID cannot read /app/app — rebuild the image (docker compose up -d --build)." >&2
    exit 1
fi

echo "[entrypoint] Starting as appuser (uid=$PUID gid=$PGID, TZ=$TZ)."
exec gosu appuser "$@"
