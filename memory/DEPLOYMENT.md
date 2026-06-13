# Deployment Guide

## Prerequisites
- VPS: 2.134.15.37 (Ubuntu 24.04, root access)
- Docker installed (already present on VPS)
- /opt/formbot/ directory with all project files
- /opt/formbot/.env with real credentials

## Deploy from Scratch (First Time)

### 1. Expand swap (run once)
```bash
swapoff /swapfile
fallocate -l 2G /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=60' >> /etc/sysctl.conf && sysctl -p
```

### 2. Upload code
From local machine (using scp or paramiko SFTP):
```bash
scp -r FormRegistrationMakingBot/* root@2.134.15.37:/opt/formbot/
```

### 3. Create .env
```bash
cat > /opt/formbot/.env << 'EOF'
TELEGRAM_BOT_TOKEN=<YOUR_TELEGRAM_BOT_TOKEN>
NVIDIA_API_KEY=<YOUR_NVIDIA_API_KEY>
DATABASE_PATH=/data/memory.db
LOG_LEVEL=INFO
EOF
```

### 4. Build and start
```bash
cd /opt/formbot
docker build -t formbot-formbot:latest .
docker compose up -d
docker logs -f formbot
```

---

## Update Code After Changes

### If only Python files changed (fast — ~3 seconds)
```bash
# Upload changed file(s) then:
cd /opt/formbot
docker compose down
docker build -t formbot-formbot:latest .   # uses cached layers, only COPY . . reruns
docker compose up -d
```

### If requirements.txt changed (medium — ~30 seconds)
```bash
# Same as above — pip install layer will re-run
cd /opt/formbot && docker compose down && docker build -t formbot-formbot:latest . && docker compose up -d
```

### If base image needs update (slow — ~10 minutes, downloads ~800MB)
```bash
docker pull mcr.microsoft.com/playwright/python:v1.52.0-noble
cd /opt/formbot && docker compose down && docker build --no-cache -t formbot-formbot:latest . && docker compose up -d
```

---

## Monitoring
```bash
# Live logs
docker logs -f formbot

# Container status and RAM usage
docker stats formbot --no-stream

# Check RAM + swap
free -h

# SQLite: view all saved user profiles
docker exec formbot sqlite3 /data/memory.db "SELECT user_id, field_key, value FROM profile ORDER BY user_id, field_key;"

# SQLite: view submission history
docker exec formbot sqlite3 /data/memory.db "SELECT * FROM form_submissions ORDER BY submitted_at DESC LIMIT 20;"

# SQLite: clear a user's session (if stuck)
docker exec formbot sqlite3 /data/memory.db "DELETE FROM sessions WHERE user_id=<telegram_user_id>;"
```

---

## Backup the Database
```bash
# On VPS:
docker exec formbot sqlite3 /data/memory.db ".backup /data/memory.db.bak"

# Copy to local:
scp root@2.134.15.37:/var/lib/docker/volumes/formbot_formbot_data/_data/memory.db ./memory_backup.db
```

---

## docker-compose.yml Notes
- Image name: `formbot-formbot` (Docker Compose prefixes project name)
- Container name: `formbot`
- Volume: `formbot_formbot_data` → mounted at `/data` inside container
- mem_limit: 850 MB, memswap_limit: 1700 MB (relies on 2GB swap)
- restart: unless-stopped (auto-restarts on crash)
- No ports exposed (Telegram long-polling only)

---

## Scaling Up (Future)
- Add more asyncio.Queue workers in queue_worker.py (change `worker()` to `worker(n=2)` or run multiple tasks)
- Migrate SQLite to Upstash Redis (swap memory.py CRUD only)
- Use Browserbase instead of local Chromium for anti-bot protection
- Move to larger VPS (e2-small 2GB RAM) to remove swap dependency
