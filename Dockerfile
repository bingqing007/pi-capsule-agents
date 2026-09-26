FROM python:3.12.10-alpine3.21
WORKDIR /app
COPY capsule/worker.py /app/worker.py
USER 65534:65534
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
ENTRYPOINT ["python", "-I", "-u", "/app/worker.py"]
