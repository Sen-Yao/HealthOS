FROM python:3.12-slim

WORKDIR /app

COPY scripts/healthos.py /app/healthos.py
COPY scripts/training-demo.html /app/training-demo.html
COPY scripts/training.html /app/training.html
COPY scripts/training-ui.js /app/training-ui.js
RUN chmod 0550 /app/healthos.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/healthos.py"]
