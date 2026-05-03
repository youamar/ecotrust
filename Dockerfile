FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend ./backend
COPY edge ./edge
COPY tests ./tests

# Default DB is at /data/ecotrust.db so the volume persists state.
ENV ECOTRUST_DB=/data/ecotrust.db
RUN mkdir -p /data

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
CMD ["/usr/local/bin/docker-entrypoint.sh"]
