# Adytum Backend API Dockerfile
# ==============================
# FastAPI server that bridges frontend to TEE worker
#
# Build:
#   docker build -t adytum-backend .
#
# Run:
#   docker run -p 8000:8000 \
#     -e CONTRACT_ADDRESS=0x... \
#     -e TEE_WORKER_URL=http://tee-worker:8001 \
#     -e RPC_URL=https://sepolia.base.org \
#     adytum-backend

FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create non-root user
RUN groupadd -r api && useradd -r -g api api

# Create app directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY api.py .

# Set ownership
RUN chown -R api:api /app

# Switch to non-root user
USER api

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=5).raise_for_status()"

# Default environment variables
ENV HOST=0.0.0.0 \
    PORT=8000 \
    RPC_URL=https://sepolia.base.org \
    IPFS_GATEWAY=https://ipfs.io/ipfs/ \
    CORS_ORIGINS=*

# Run the server
CMD ["python", "api.py"]
