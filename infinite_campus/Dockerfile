ARG BUILD_FROM
FROM ${BUILD_FROM}

# Copy application code
COPY app/ /app/

# Make run script executable
RUN chmod a+x /app/run.sh

# Create data directory
RUN mkdir -p /data

WORKDIR /app

# Install dependencies at build time (with multiple fallbacks)
RUN apk add --no-cache py3-pip python3 || true
RUN pip3 install --no-cache-dir --break-system-packages aiohttp==3.9.5 beautifulsoup4==4.12.3 || \
    pip3 install --no-cache-dir aiohttp==3.9.5 beautifulsoup4==4.12.3 || \
    python3 -m pip install --no-cache-dir aiohttp==3.9.5 beautifulsoup4==4.12.3 || true

# Set labels
LABEL \
    io.hass.name="Infinite Campus Monitor" \
    io.hass.description="Monitor student grades, assignments, and attendance from Infinite Campus with WhatsApp notifications" \
    io.hass.type="addon" \
    io.hass.version="1.3.4"

# Run via startup script which also installs deps as fallback
CMD ["/app/run.sh"]
