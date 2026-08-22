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

RUN groupadd --system assistant \
    && useradd --system --gid assistant --home-dir /app assistant \
    && mkdir -p /data/generated \
    && chown -R assistant:assistant /data

WORKDIR /app

COPY pyproject.toml README.md ./

# Install third-party dependencies before application sources so routine code changes
# do not invalidate the dependency and local embedding-model layers.
RUN mkdir -p assistant_app \
    && touch assistant_app/__init__.py \
    && python -m pip install . \
    && rm -rf assistant_app

RUN mkdir -p /opt/fastembed \
    && python -c "import os,tarfile,urllib.request; archive='/tmp/bge-small-zh-v1.5.tar.gz'; urllib.request.urlretrieve('https://storage.googleapis.com/qdrant-fastembed/fast-bge-small-zh-v1.5.tar.gz', archive); tarfile.open(archive, 'r:gz').extractall('/opt/fastembed', filter='data'); os.unlink(archive)" \
    && python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-zh-v1.5', cache_dir='/opt/fastembed', local_files_only=True)"

COPY assistant_app ./assistant_app
COPY alembic.ini ./
COPY migrations ./migrations

RUN chmod -R a+rX /app \
    && python -m pip install --no-deps .

ENV ASSISTANT_MEMORY_EMBEDDING_PROVIDER=local \
    ASSISTANT_MEMORY_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5 \
    ASSISTANT_MEMORY_EMBEDDING_CACHE=/opt/fastembed \
    ASSISTANT_MEMORY_EMBEDDING_LOCAL_FILES_ONLY=true \
    ASSISTANT_MEMORY_EMBEDDING_THREADS=2

USER assistant
EXPOSE 8000 19000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3)"]

CMD ["uvicorn", "assistant_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
