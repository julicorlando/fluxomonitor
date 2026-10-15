FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
COPY collector/requirements.txt /app/collector-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r collector-requirements.txt
COPY . /app
ENV OPENCV_LOG_LEVEL=SILENT OPENCV_FFMPEG_LOGLEVEL=-8 OMP_THREAD_LIMIT=1
CMD ["python", "manage.py", "run_plate_worker"]
