# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
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

# Keep the converter focused on the formats exposed by the product. The upstream
# markitdown-mcp alpha currently pulls every optional Azure/audio dependency.
RUN python -m pip install \
    "mcp>=1.29,<2" \
    "markitdown[pdf,docx,pptx,xlsx]>=0.1,<1"

COPY --chown=markitdown:markitdown docker/markitdown-mcp/server.py ./server.py

USER markitdown
EXPOSE 3001

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import socket; s=socket.create_connection(('127.0.0.1',3001),3); s.close()"]

CMD ["python", "server.py"]
