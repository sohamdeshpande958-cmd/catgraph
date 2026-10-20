FROM python:3.12-slim

LABEL org.opencontainers.image.title="CatGraph" \
      org.opencontainers.image.description="Local store for categories that form a DAG and entries in multiple categories. SQLite + FastAPI + browser GUI." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Soham Deshpande" \
      org.opencontainers.image.source="https://github.com/sohamdeshpande958-cmd/catgraph" \
      org.opencontainers.image.url="https://github.com/sohamdeshpande958-cmd/catgraph"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/catgraph.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY LICENSE app.py ./
COPY static ./static

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
