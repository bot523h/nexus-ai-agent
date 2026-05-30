#!/data/data/com.termux/files/usr/bin/bash
set -e
echo "🚀 NEXUS AI Setup for Termux"
pkg update -y && pkg upgrade -y
pkg install -y python git curl
git clone https://github.com/bot523h/nexus-ai-agent
cd nexus-ai-agent
pip install -r requirements.txt
cp .env.example .env
echo "✅ آماده! .env رو ویرایش کن:"
echo "nano .env"
echo "بعد: python -m nexus_ai_agent.cli run-bot"
