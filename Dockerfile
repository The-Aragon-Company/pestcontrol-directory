# Portable web image (serves the directory; scraper runs separately in CI).
FROM python:3.12-slim
WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt
COPY web/ web/
COPY scraper/db.py scraper/db.py
COPY data/pestcontrol.db data/pestcontrol.db
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "gunicorn web.wsgi:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60"]
