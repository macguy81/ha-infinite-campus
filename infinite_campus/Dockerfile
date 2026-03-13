ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python and dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-aiohttp \
    && pip3 install --no-cache-dir --break-system-packages \
    beautifulsoup4>=4.12.0

# Copy application code
COPY app/ /app/

# Create data directory
RUN mkdir -p /data

WORKDIR /app

# Set labels
LABEL \
    io.hass.name="Infinite Campus Monitor" \
    io.hass.description="Monitor student grades, assignments, and attendance from Infinite Campus with WhatsApp notifications" \
    io.hass.type="addon" \
    io.hass.version="1.0.0"

# Run the web server
CMD ["python3", "/app/server.py"]
