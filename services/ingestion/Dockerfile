FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY services/ingestion/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY services/ingestion/ingestion_service ./ingestion_service
COPY services/ingestion/alembic.ini services/ingestion/entrypoint.sh ./
COPY services/ingestion/migrations ./migrations
# a fresh named volume copies ownership from the image, so appuser can write /models
RUN useradd -m appuser && mkdir -p /models && chown appuser /models
USER appuser
EXPOSE 8001
CMD ["sh", "/srv/entrypoint.sh"]
