FROM python:3.12-slim

WORKDIR /app

COPY deploy/healthos/scripts/healthos.py /app/healthos.py
COPY deploy/healthos/scripts/training-demo.html /app/training-demo.html
COPY deploy/healthos/scripts/training.html /app/training.html
COPY deploy/healthos/scripts/training-ui.js /app/training-ui.js
RUN chmod 0550 /app/healthos.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/healthos.py"]
