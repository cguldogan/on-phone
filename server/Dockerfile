FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /bin/uv
WORKDIR /app
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server/agent_phone ./agent_phone
COPY models/silero_vad.onnx ./models/silero_vad.onnx
RUN useradd -u 1000 -m phone && mkdir -p /data && chown phone:phone /data
USER phone
EXPOSE 8765 40000-40019/udp
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2)"
CMD ["/app/.venv/bin/python", "-m", "agent_phone"]
