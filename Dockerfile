# knowledge-mcp Docker image
# MCP mode: docker run -v ~/kb:/app/kb knowledge-mcp
# Web mode:  docker run -p 8000:8000 -v ~/kb:/app/kb knowledge-mcp web
FROM python:3.12-slim

WORKDIR /app

COPY . .

# Use uv for much faster, lockfile-driven installs
RUN pip install --no-cache-dir uv \
    && uv pip install --system --no-cache -e .

# Expose port for web mode
EXPOSE 8000

# Default: run MCP server with base dir /app/kb (override with: shell, create mykb, list, web, etc.)
ENTRYPOINT ["python", "-m", "knowledge_mcp.cli", "--base", "/app/kb"]
