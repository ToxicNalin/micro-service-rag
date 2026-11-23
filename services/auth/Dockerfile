FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY services/auth/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY services/auth/auth_service ./auth_service
COPY services/auth/alembic.ini services/auth/entrypoint.sh ./
COPY services/auth/migrations ./migrations
# a fresh named volume copies ownership from the image, so appuser can write /keys
RUN useradd -m appuser && mkdir -p /keys && chown appuser /keys
USER appuser
EXPOSE 8003
CMD ["sh", "/srv/entrypoint.sh"]
