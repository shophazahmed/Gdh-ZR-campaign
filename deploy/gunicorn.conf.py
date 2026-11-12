import multiprocessing
import os

# Gunicorn's control socket lives in ~/.gunicorn; the service user's home may not be writable.
if not os.access(os.path.expanduser("~"), os.W_OK):
    os.environ["HOME"] = "/tmp"

bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8001")
workers = int(os.environ.get("GUNICORN_WORKERS", min(4, multiprocessing.cpu_count() * 2 + 1)))
threads = 2
timeout = 60
graceful_timeout = 30
max_requests = 2000
max_requests_jitter = 200
accesslog = "-"
errorlog = "-"
# Nginx is on the same host; only trust X-Forwarded-* from it.
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
