FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p recordings/user recordings/agent recordings/analysis

EXPOSE 7035

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7035"]
