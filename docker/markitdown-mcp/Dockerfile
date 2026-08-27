# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=120

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT}

RUN groupadd --system markitdown \
    && useradd --system --gid markitdown --home-dir /srv/markitdown markitdown \
    && mkdir -p /srv/markitdown /data/generated \
    && chown -R markitdown:markitdown /srv/markitdown

WORKDIR /srv/markitdown

# The official package pins the compatible MCP SDK and MarkItDown extras itself.
RUN python -m pip install "markitdown-mcp==0.0.1a4"

USER markitdown
EXPOSE 3001

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import socket; s=socket.create_connection(('127.0.0.1',3001),3); s.close()"]

CMD ["markitdown-mcp", "--http", "--host", "0.0.0.0", "--port", "3001"]
