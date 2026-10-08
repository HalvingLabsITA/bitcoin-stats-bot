# ── Halving Labs Bitcoin Bot — Dockerfile ──────────────────────────────────────
# Immagine unica per bot.py (Telegram) e server.py (FastAPI dashboard)
# Ubuntu 24.04 compatible | Python 3.11 slim

FROM python:3.11-slim

# Evita prompt interattivi durante apt
ENV DEBIAN_FRONTEND=noninteractive

# Dipendenze di sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Directory di lavoro
WORKDIR /app

# Installa prima le dipendenze Python (layer cachato)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il codice
COPY bot.py .
COPY server.py .
COPY dashboard.html .

# Porta della dashboard web
EXPOSE 8000

# Default: bot Telegram
# Sovrascritto dal docker-compose per il servizio dashboard
CMD ["python", "bot.py"]
