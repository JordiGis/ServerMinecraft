FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

EXPOSE 8000

# 1 worker: los stats de Docker y RCON son ligeros; threads para concurrencia
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "--timeout", "600", "app:app"]
