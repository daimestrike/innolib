# Образ без pip install: приложению нужна только стандартная библиотека Python.
# В закрытом контуре укажите базовый образ из внутреннего реестра:
#   docker build --build-arg BASE_IMAGE=registry.x5.ru/python:3.12-slim -t innolib .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG VERSION=dev
LABEL org.opencontainers.image.title="innolib" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/daimestrike/innolib"

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/innolib.sqlite3

WORKDIR /app
COPY *.py VERSION ./
COPY static ./static
COPY data/seed_demo.json ./data/seed_demo.json

RUN useradd --system --uid 10001 innolib && mkdir -p /data && chown innolib /data
USER innolib
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1

CMD ["python3", "app.py"]
