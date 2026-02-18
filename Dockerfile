# Multi-stage build for optimized production image
FROM python:3.11-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files
COPY pyproject.toml ./
COPY fusion/ ./fusion/

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[all]"

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN groupadd -r fusion && useradd -r -g fusion fusion

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY fusion/ ./fusion/
COPY pyproject.toml ./

# Install in production mode
RUN pip install --no-cache-dir -e ".[all]"

# Create directories for data persistence
RUN mkdir -p /app/data /app/logs && \
    chown -R fusion:fusion /app

# Switch to non-root user
USER fusion

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${PORT:-9000}/health || exit 1

# Expose port
EXPOSE 9000

# Default command (can be overridden in docker-compose)
CMD ["fusion-rest", "--warp-url", "${WARP_URL:-http://warp:8000}", "--port", "9000"]
