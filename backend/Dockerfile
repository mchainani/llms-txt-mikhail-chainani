FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UVICORN_LOG_CONFIG=/app/logging.json \
    LOG_FORMAT="%(asctime)s %(levelname)s [%(name)s] %(message)s"

WORKDIR /app

COPY pyproject.toml requirements.lock README.md logging.json ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir -c requirements.lock . \
    && useradd --create-home --uid 10001 app \
    && chown app:app /app

COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

USER app
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["server"]
