#!/usr/bin/env bash
# ── NEXUS AI Agent — Termux Installer ──
# Usage: bash scripts/termux_install.sh
# Installs runtime dependencies and sets up the bot on Termux (Android).
# The bot is started through the single supported entrypoint:
#   python -m nexus_ai_agent.cli run-bot --mode polling

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[NEXUS]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }

# ── 1. Update packages ──
log "Updating Termux packages..."
pkg update -y && pkg upgrade -y

# ── 2. Install system dependencies ──
# build-essential/cmake: needed by source-built wheels (e.g. llama-cpp-python).
log "Installing system dependencies..."
pkg install -y python python-pip git sqlite ffmpeg build-essential cmake pkg-config

# ── 3. Clone repository ──
REPO_URL="https://github.com/bot523h/nexus-ai-agent.git"
INSTALL_DIR="$HOME/nexus-ai-agent"

if [ -d "$INSTALL_DIR" ]; then
    warn "Directory $INSTALL_DIR already exists. Pulling latest..."
    cd "$INSTALL_DIR" && git pull origin main
else
    log "Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── 4. Install Python dependencies ──
# Runtime install only (not [dev]): a phone is a deployment target,
# not a development workstation. Heavy optional stacks (torch via
# sentence-transformers, local llama.cpp) may take a long time to compile.
log "Installing Python runtime dependencies..."
pip install --upgrade pip
pip install -e .

# ── 5. Create data directory ──
mkdir -p data

# ── 6. Setup .env file if not exists ──
if [ ! -f .env ]; then
    warn "No .env file found. Creating from .env.example..."
    if [ -f .env.example ]; then
        cp .env.example .env
        warn "Please edit .env with your API keys before starting!"
        info "Run: nano $INSTALL_DIR/.env"
    else
        warn "No .env.example found. Please create .env manually."
    fi
fi

# ── 7. Create startup script ──
# Canonical entrypoint + the exact env-var names config/settings.py reads
# (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, NEXUS_OWNER_TELEGRAM_ID).
cat > "$INSTALL_DIR/start.sh" << 'STARTSCRIPT'
#!/usr/bin/env bash
cd "$(dirname "$0")"
set -a
. ./.env 2>/dev/null || true
set +a
python -m nexus_ai_agent.cli run-bot --mode polling
STARTSCRIPT
chmod +x "$INSTALL_DIR/start.sh"

# ── 8. Create autostart service ──
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-nexus.sh" << 'BOOTSCRIPT'
#!/usr/bin/env bash
cd "$HOME/nexus-ai-agent"
bash start.sh > data/boot.log 2>&1 &
BOOTSCRIPT
chmod +x "$HOME/.termux/boot/start-nexus.sh" 2>/dev/null || true

log "✅ Installation complete!"
echo ""
info "Quick Start:"
info "  1. Edit .env:  nano $INSTALL_DIR/.env"
info "  2. Start bot:  bash $INSTALL_DIR/start.sh"
info "  3. Auto-start on boot is configured (requires Termux:Boot)"
echo ""
warn "To enable auto-start on device boot:"
warn "  Install Termux:Boot from F-Droid and run: termux-setup-boot"
