# One image, two roles:
#   server: igepn-mcp serve --transport http --host 0.0.0.0   (IGEPN_MCP_TOKEN set; IGEPN_POLL_MINUTES=3 polls in-process)
#   poll:   igepn-mcp poll                                    (alternative: a host timer every 3 min, same /data volume)
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock LICENSE README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" IGEPN_DB=/data/igepn.db PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status!=200)"
ENTRYPOINT ["igepn-mcp"]
CMD ["serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
