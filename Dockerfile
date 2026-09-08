FROM python:3.11-slim

LABEL org.opencontainers.image.title="workbuddy2api-python" \
      org.opencontainers.image.description="OpenAI-compatible reverse proxy for Tencent Copilot / WorkBuddy" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WB2A_LISTEN=:7863 \
    WB2A_AUTH_DIR=/app/auths \
    WB2A_STATE_FILE=/app/data/state.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY wb2api/ ./wb2api/
COPY cli/ ./cli/
COPY config.example.json ./

RUN mkdir -p /app/auths /app/data

# 7863 为默认监听端口
EXPOSE 7863

VOLUME ["/app/auths", "/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7863/healthz', timeout=3)" || exit 1

CMD ["python", "cli/server.py", "-config", "/app/config.json"]
