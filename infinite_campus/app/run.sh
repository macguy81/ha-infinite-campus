#!/usr/bin/env bash
set -e

echo "=== Infinite Campus Monitor Starting ==="

# Install Python dependencies at runtime
echo "Installing Python dependencies..."
pip3 install --no-cache-dir --break-system-packages aiohttp==3.9.5 beautifulsoup4==4.12.3 2>&1 || \
pip3 install --no-cache-dir aiohttp==3.9.5 beautifulsoup4==4.12.3 2>&1 || \
python3 -m pip install --no-cache-dir aiohttp==3.9.5 beautifulsoup4==4.12.3 2>&1

echo "Dependencies installed successfully"

# Create data directory
mkdir -p /data

# Start the web server
echo "Starting web server..."
exec python3 /app/server.py
