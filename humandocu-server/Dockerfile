FROM python:3.11-slim

WORKDIR /app

COPY humandocu-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY humandocu-server/ .

CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1
