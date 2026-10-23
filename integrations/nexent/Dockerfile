FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai

WORKDIR /app

RUN groupadd --system nexent \
    && useradd --system --gid nexent --home-dir /app nexent

COPY integrations/nexent/mcp_server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY integrations/nexent/mcp_server ./mcp_server

USER nexent

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import socket; s=socket.create_connection(('127.0.0.1', 8100), 3); s.close()"]

CMD ["python", "mcp_server/server.py"]
