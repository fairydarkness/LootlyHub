FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd -m hub && mkdir -p storage logs goods plugins && chown -R hub /app
USER hub

VOLUME ["/app/configs", "/app/goods", "/app/storage", "/app/logs", "/app/plugins"]
CMD ["python", "main.py"]
