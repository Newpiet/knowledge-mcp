# knowledge-mcp Docker image
# MCP mode: docker run -v ~/kb:/app/kb knowledge-mcp
# Web mode:  docker run -p 8000:8000 -v ~/kb:/app/kb knowledge-mcp web
FROM python:3.12-slim

# System dependencies required by mineru (PDF parser) and other libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

# Install CPU-only PyTorch first to prevent CUDA packages being pulled in
# Then install project deps via uv using Chinese mirrors (for China servers)
RUN pip install --no-cache-dir uv -i https://mirrors.aliyun.com/pypi/simple/ \
    && uv pip install --system --no-cache \
        torch --index-url https://download.pytorch.org/whl/cpu \
    && uv pip install --system --no-cache -e . \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/

# Expose port for web mode
EXPOSE 8000

# Default: run MCP server with base dir /app/kb
ENTRYPOINT ["python", "-m", "knowledge_mcp.cli", "--base", "/app/kb"]
