FROM python:3.12-slim

WORKDIR /app

ARG HEALTHOS_VERSION=dev
ARG HEALTHOS_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/Sen-Yao/HealthOS" \
      org.opencontainers.image.version="${HEALTHOS_VERSION}" \
      org.opencontainers.image.revision="${HEALTHOS_REVISION}"

COPY deploy/healthos/scripts/healthos.py /app/healthos.py
COPY deploy/healthos/scripts/training.html /app/training.html
COPY deploy/healthos/scripts/training-ui.js /app/training-ui.js
RUN chmod 0550 /app/healthos.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/healthos.py"]
