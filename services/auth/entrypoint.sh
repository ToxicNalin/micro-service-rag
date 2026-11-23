#!/bin/sh
set -e
alembic upgrade head
exec uvicorn auth_service.main:app --host 0.0.0.0 --port 8003 --proxy-headers
