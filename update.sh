#!/bin/bash
set -e

echo "============================================="
echo "   🚀 Updating Discord TTS Bot on Cloud      "
echo "============================================="

cd "$(dirname "$0")"

echo "[*] Pulling latest code from GitHub..."
git pull origin main

echo "[*] Updating Python dependencies..."
if [ -d ".venv" ]; then
    .venv/bin/pip install -r requirements.txt --quiet
else
    pip3 install -r requirements.txt --quiet
fi

echo "[*] Restarting discord-tts service..."
if command -v systemctl &> /dev/null; then
    sudo systemctl restart discord-tts
    echo "✅ Service restarted! Checking status..."
    sudo systemctl status discord-tts --no-pager -n 10
else
    echo "⚠️ systemctl not found, please restart your process manually."
fi

echo "============================================="
echo "   🎉 Update Completed Successfully!        "
echo "============================================="
