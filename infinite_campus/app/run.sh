#!/usr/bin/env bash
echo "=== Infinite Campus Monitor v1.2.5 Starting ==="

# Install Python dependencies at runtime (fallback if Docker build missed them)
echo "Checking/installing Python dependencies..."
apk add --no-cache py3-pip python3 2>/dev/null || true
pip3 install --no-cache-dir --break-system-packages aiohttp==3.9.5 beautifulsoup4==4.12.3 2>&1 || \
  pip3 install --no-cache-dir aiohttp==3.9.5 beautifulsoup4==4.12.3 2>&1 || \
  python3 -m pip install --no-cache-dir aiohttp==3.9.5 beautifulsoup4==4.12.3 2>&1 || \
  echo "WARNING: Failed to install dependencies"

echo "Verifying aiohttp is available..."
python3 -c "import aiohttp; print('aiohttp version:', aiohttp.__version__)" 2>&1 || echo "ERROR: aiohttp still not available"

# Create data directory
mkdir -p /data

# Start the web server
echo "Starting web server on port 8099..."
exec python3 /app/server.py
