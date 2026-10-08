#!/bin/bash
# ── Halving Labs — Script di deploy VPS (Ubuntu 24.04) ─────────────────────────
# Uso: bash deploy.sh
# Esegui come root o utente con sudo

set -e

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Halving Labs — Deploy VPS Ubuntu 24.04  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Aggiornamento sistema ──────────────────────────────────────────────────
echo "📦 Aggiornamento sistema..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq

# ── 2. Installazione Docker ───────────────────────────────────────────────────
if ! command -v docker &> /dev/null; then
    echo "🐳 Installazione Docker..."
    sudo apt-get install -y -qq ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
        sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
        sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    sudo systemctl enable docker
    sudo systemctl start docker
    # Permetti all'utente corrente di usare docker senza sudo
    sudo usermod -aG docker "$USER"
    echo "✅ Docker installato"
else
    echo "✅ Docker già installato"
fi

# ── 3. Struttura cartelle ─────────────────────────────────────────────────────
echo "📁 Creazione struttura cartelle..."
mkdir -p ~/halving-bot/data
cd ~/halving-bot

# ── 4. File .env ─────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo ""
    echo "🔑 Inserisci il token del bot Telegram:"
    read -r BOT_TOKEN_INPUT
    echo "🔑 Inserisci il tuo user ID Telegram (admin):"
    read -r ADMIN_ID_INPUT
    echo "BOT_TOKEN=${BOT_TOKEN_INPUT}" > .env
    echo "ADMIN_ID=${ADMIN_ID_INPUT}" >> .env
    echo "✅ File .env creato"
else
    echo "✅ File .env già presente"
fi

# ── 5. Build e avvio ──────────────────────────────────────────────────────────
echo ""
echo "🔨 Build immagine Docker..."
sudo docker compose build --no-cache

echo ""
echo "🚀 Avvio servizi..."
sudo docker compose up -d

# ── 6. Verifica ───────────────────────────────────────────────────────────────
echo ""
echo "⏳ Attendo avvio (10 secondi)..."
sleep 10

echo ""
echo "📊 Stato containers:"
sudo docker compose ps

echo ""
echo "📋 Ultimi log del bot:"
sudo docker compose logs bot --tail 15

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║            Deploy completato! ✅          ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "📌 Comandi utili:"
echo "   Log bot:       sudo docker compose logs -f bot"
echo "   Log dashboard: sudo docker compose logs -f dashboard"
echo "   Stop:          sudo docker compose down"
echo "   Riavvio bot:   sudo docker compose restart bot"
echo "   Dashboard:     http://$(curl -s ifconfig.me 2>/dev/null || echo 'IP-VPS'):8000"
echo ""
