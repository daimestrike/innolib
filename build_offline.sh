#!/usr/bin/env bash
# Собирает всё для переноса в закрытый контур.
# Запускать на машине с интернетом (или с доступом к реестру образов).
#   ./build_offline.sh  -> dist/innolib-<версия>.tar.gz (+ dist/innolib-image-<версия>.tar.gz, если доступен docker)
set -euo pipefail
cd "$(dirname "$0")"
VER=$(cat VERSION)
mkdir -p dist
tar --exclude=./dist --exclude=./.git --exclude=./.github --exclude='./data/*.sqlite3*' \
    --exclude=__pycache__ --exclude=./.env \
    --transform "s,^\.,innolib-$VER," -czf "dist/innolib-$VER.tar.gz" .
echo "Исходники: dist/innolib-$VER.tar.gz"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker build --build-arg BASE_IMAGE="${BASE_IMAGE:-python:3.12-slim}" --build-arg VERSION="$VER" \
    -t "innolib:$VER" -t innolib:latest .
  docker save "innolib:$VER" innolib:latest | gzip > "dist/innolib-image-$VER.tar.gz"
  echo "Образ: dist/innolib-image-$VER.tar.gz  (в контуре: docker load -i innolib-image-$VER.tar.gz)"
else
  echo "docker недоступен — образ не собран, соберите его в контуре из исходников."
fi
