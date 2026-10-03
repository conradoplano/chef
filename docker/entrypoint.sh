#!/bin/sh
set -e

# Started as root: make sure the mounted data folder is writable for the app
# user (a bind mount on the NAS is usually created as root), then drop privileges.
if [ "$(id -u)" = "0" ]; then
    chown -R app:app "$DATA_DIR"
    export HOME=/app
    exec setpriv --reuid=app --regid=app --init-groups "$0" "$@"
fi

python manage.py migrate --noinput

# Optionally bootstrap the first admin user, e.g. INITIAL_ADMIN_EMAIL=you@example.com
if [ -n "$INITIAL_ADMIN_EMAIL" ]; then
    python manage.py adduser "$INITIAL_ADMIN_EMAIL" --admin
fi

exec "$@"
