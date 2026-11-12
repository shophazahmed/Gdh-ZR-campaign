FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    VOTER_PORTAL_DATA_DIR=/data GUNICORN_BIND=0.0.0.0:8000
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home /data voterportal
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY deploy/gunicorn.conf.py ./deploy/gunicorn.conf.py
COPY wsgi.py .
RUN install -d -o voterportal -g voterportal -m 700 /data
USER voterportal
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login')"
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "wsgi:app"]
