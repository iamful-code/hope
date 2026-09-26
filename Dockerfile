# Образ движка и монитора hope. Один образ, разные команды (см. docker-compose.yml).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY strategies ./strategies
RUN uv pip install --system -e .

VOLUME ["/app/data"]
EXPOSE 8000
ENTRYPOINT ["hope"]
CMD ["--help"]
