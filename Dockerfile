# Remote MCP server for Azure Container Apps (Streamable HTTP on port 8000).
#   docker build -t ssis-adf-agent .
#   docker run -p 8000:8000 -e MCP_API_KEYS=<long-random-key> \
#              -e AZURE_STORAGE_ACCOUNT=<account> ssis-adf-agent
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: optional "git" package source for scan_ssis_packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY ssis_adf_agent ./ssis_adf_agent
RUN pip install . && useradd --create-home --uid 10001 mcp

USER mcp
ENV MCP_TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)"

CMD ["ssis-adf-agent"]
