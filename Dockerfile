FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    THREADS=16

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/backups && useradd -m -u 1000 app && chown -R app:app /app
USER app

# Render sets PORT (default 10000). ONE worker + threads: background jobs (outbox, cleanup, game sweep) live in this process.
EXPOSE 10000
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','10000'),timeout=8)"

CMD ["sh", "-c", "exec gunicorn app:app --workers 1 --threads ${THREADS} --timeout 120 --graceful-timeout 30 --worker-tmp-dir /dev/shm --bind 0.0.0.0:${PORT:-10000}"]
