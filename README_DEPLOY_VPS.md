# Halving Labs — Deploy su VPS (Ubuntu 24.04)

## Prerequisiti
- VPS con Ubuntu 24.04
- Accesso SSH come root o utente con sudo
- Token del bot Telegram (da @BotFather)

---

## Deploy automatico (consigliato)

```bash
# 1. Connettiti al VPS
ssh root@IP-DEL-VPS

# 2. Copia i file sul VPS (esegui dal tuo Mac)
scp -r "/percorso/locale/halving-bot/." root@IP-DEL-VPS:~/halving-bot/

# 3. Connettiti e avvia lo script
ssh root@IP-DEL-VPS
cd ~/halving-bot
chmod +x deploy.sh
bash deploy.sh
```

Lo script installa Docker, crea il file `.env` con il tuo token e avvia tutto.

---

## Deploy manuale (passo-passo)

### 1. Copia i file sul VPS
```bash
# Dal tuo Mac:
scp -r "/percorso/locale/halving-bot/." root@IP-DEL-VPS:~/halving-bot/
```

### 2. Connettiti al VPS
```bash
ssh root@IP-DEL-VPS
cd ~/halving-bot
```

### 3. Installa Docker
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 4. Crea il file .env
```bash
cp .env.example .env
nano .env
# Inserisci BOT_TOKEN e ADMIN_ID (il tuo ID Telegram)
```

### 5. Crea la cartella dati
```bash
mkdir -p data
```

### 6. Build e avvio
```bash
sudo docker compose build
sudo docker compose up -d
```

### 7. Verifica
```bash
sudo docker compose ps
sudo docker compose logs bot --tail 20
```

---

## Comandi utili

| Azione | Comando |
|--------|---------|
| Vedere log bot in tempo reale | `sudo docker compose logs -f bot` |
| Vedere log dashboard | `sudo docker compose logs -f dashboard` |
| Riavviare solo il bot | `sudo docker compose restart bot` |
| Fermare tutto | `sudo docker compose down` |
| Aggiornare dopo modifiche al codice | `sudo docker compose build && sudo docker compose up -d` |
| Stato containers | `sudo docker compose ps` |

---

## Struttura cartelle sul VPS

```
~/halving-bot/
├── bot.py               # Bot Telegram
├── server.py            # Backend FastAPI
├── dashboard.html       # Frontend web
├── Dockerfile           # Immagine Docker
├── docker-compose.yml   # Orchestrazione
├── requirements.txt     # Dipendenze Python
├── .env                 # Token (NON committare su git!)
├── .env.example         # Template
└── data/                # File persistenti (volume Docker)
    ├── bot.log          # Log del bot
    ├── last_block.txt   # Ultimo blocco notificato
    └── alerts.json      # Alert utenti salvati
```

---

## Dashboard web

Dopo il deploy, la dashboard è accessibile da browser:
```
http://IP-DEL-VPS:8000
```

Se vuoi usare un dominio con HTTPS, configura Nginx come reverse proxy.

---

## Aggiornare il codice

```bash
# Dal Mac — copia i file aggiornati:
scp "/percorso/locale/halving-bot/bot.py" root@IP-DEL-VPS:~/halving-bot/bot.py
scp "/percorso/locale/halving-bot/server.py" root@IP-DEL-VPS:~/halving-bot/server.py

# Sul VPS — rebuilda e riavvia:
ssh root@IP-DEL-VPS "cd ~/halving-bot && sudo docker compose build && sudo docker compose up -d"
```
