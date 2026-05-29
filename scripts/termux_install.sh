#!/usr/bin/env bash
# ── NEXUS AI Agent v3.0.0 — Termux Installer ──
# Usage: bash termux_install.sh
# Installs all dependencies and sets up the bot on Termux (Android).

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
log "Installing system dependencies..."
pkg install -y python python-pip git sqlite ffmpeg

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
log "Installing Python dependencies..."
pip install --upgrade pip
pip install -e ".[dev]"

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
cat > "$INSTALL_DIR/start.sh" << 'STARTSCRIPT'
#!/usr/bin/env bash
cd "$(dirname "$0")"
source .env 2>/dev/null || true
export TELEGRAM_TOKEN TELEGRAM_BOT_NAME GEMINI_API_KEY OWNER_TEGRAM_ID
python -m nexus_ai_agent.bot.main
STARTSCRIPT
chmod +x "$INSTALL_DIR/start.sh"

# ── 8. Create autostart service ──
mkdir -p "$HOME/.termux"
cat > "$HOME/.termux/boot/start-nexus.sh" << 'BOOTSCRIPT'
#!/usr/bin/env bash
cd "$HOME/nexus-ai-agent"
bash start.sh &
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
