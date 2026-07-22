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

groupmod -o -g "$PGID" appuser
usermod -o -u "$PUID" -g "$PGID" appuser

# config is ours; downloads may hold thousands of files shared with Calibre,
# so only the directory itself is adjusted, never its contents.
chown -R appuser:appuser /app/config 2>/dev/null || true
chown appuser:appuser /app/downloads 2>/dev/null || true

echo "[entrypoint] Starting as appuser (uid=$PUID gid=$PGID, TZ=$TZ)."
exec gosu appuser "$@"
