#!/bin/sh
set -eu
python manage.py migrate --noinput
python manage.py seed_cameras
python manage.py collectstatic --noinput
exec "$@"
