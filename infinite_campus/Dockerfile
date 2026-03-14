ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install pip if not present
RUN apk add --no-cache py3-pip || true

# Copy application code
COPY app/ /app/

# Make run script executable
RUN chmod +x /app/run.sh

# Create data directory
RUN mkdir -p /data

WORKDIR /app

# Set labels
LABEL \
    io.hass.name="Infinite Campus Monitor" \
    io.hass.description="Monitor student grades, assignments, and attendance from Infinite Campus with WhatsApp notifications" \
    io.hass.type="addon" \
    io.hass.version="1.0.3"

# Run via startup script which installs deps then starts server
CMD ["/app/run.sh"]
