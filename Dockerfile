FROM python:3.11-slim

# ffmpeg é necessário pro whisper ler áudio .ogg do Telegram
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render usa a porta via variável PORT
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT} app:app"]
