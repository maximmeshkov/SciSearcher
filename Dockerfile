FROM python:3.14-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 INDEXER_HOST=0.0.0.0 INDEXER_DATA_DIR=/data
COPY requirements.txt .
# djvulibre-bin provides ddjvu, which renders DjVu pages to PDF for MinerU.
RUN apt-get update \
    && apt-get install -y --no-install-recommends djvulibre-bin poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && mkdir /data \
    && chown 10001:10001 /data
COPY *.py ./
COPY sources ./sources
COPY static ./static

USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
