"""Production WSGI entrypoint.  gunicorn 'web.wsgi:app'  (run from repo root)."""
from web.app import app  # noqa: F401
