FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CATALOG_DB=/data/catalog.db \
    MAX_CREDENTIALS=2000 \
    PORT=8000

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py ./

RUN useradd --create-home --uid 10001 catalog \
    && mkdir -p /data \
    && chown -R catalog:catalog /srv/app /data

USER catalog
VOLUME ["/data"]
EXPOSE 8000

# One gunicorn worker keeps the single background solve worker deterministic.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", \
     "--timeout", "120", "--access-logfile", "-", "wsgi:app"]
