# Rebuilt from the layers of ghcr.io/fahmula/kash:latest (1.3.0); the upstream
# Dockerfile isn't published. Same base, user, port, env and healthcheck.
FROM python:3.13-slim AS builder
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

FROM python:3.13-slim
LABEL org.opencontainers.image.title="Kash" \
      org.opencontainers.image.description="Self-hosted personal finance and budgeting" \
      org.opencontainers.image.licenses="MIT" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/Fahmula/kash-assets/refs/heads/main/kash.png" \
      net.unraid.docker.webui="http://[IP]:[PORT:8000]/"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 kashuser
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app
USER kashuser
ENV ENVIRONMENT=production \
    DEBUG=false \
    SYNC_SCHEDULE_HOURS=0,4,8,12,16,20 \
    SYNC_TIMEZONE=America/New_York \
    TZ=America/New_York \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1
CMD ["uvicorn", "kash.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
