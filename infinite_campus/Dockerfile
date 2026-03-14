ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python and pip, then install Python packages
RUN apk add --no-cache \
    python3 \
    py3-pip \
    && pip3 install --no-cache-dir --break-system-packages \
    aiohttp>=3.9.0 \
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
    io.hass.version="1.0.1"

# Run the web server
CMD ["python3", "/app/server.py"]
