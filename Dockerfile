FROM python:3.13-slim

# Commit the image was built from; shown at /health/.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# collectstatic needs a SECRET_KEY to import settings; this one is build-time only.
RUN SECRET_KEY=build-only DATA_DIR=/tmp/build-data python manage.py collectstatic --noinput \
    && useradd --system --uid 1000 --home /app app \
    && mkdir -p /data && chown app /data /app \
    && chmod +x docker/entrypoint.sh

# Starts as root only to fix ownership of /data; the entrypoint then runs everything as "app".
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/')" || exit 1

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-"]
