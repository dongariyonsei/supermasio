#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/app/data}"

mkdir -p "$DATA_DIR/uploads" "$DATA_DIR/qr"

# static/uploads → persistent volume
if [ ! -L /app/static/uploads ]; then
  rm -rf /app/static/uploads
  ln -s "$DATA_DIR/uploads" /app/static/uploads
fi

# static/qr → persistent volume
if [ ! -L /app/static/qr ]; then
  rm -rf /app/static/qr
  ln -s "$DATA_DIR/qr" /app/static/qr
fi

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
