FROM postgres:16-alpine
RUN apk add --no-cache python3 py3-psycopg2 tzdata
WORKDIR /backup-worker
COPY worker.py .
ENTRYPOINT ["python3", "-u", "worker.py"]
