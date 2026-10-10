FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /bin/uv
WORKDIR /app
COPY speech/kokoro/pyproject.toml speech/kokoro/uv.lock ./
RUN uv sync --frozen --no-install-project
COPY speech/kokoro/kokoro_server.py ./
RUN useradd -u 1000 -m tts
USER tts
CMD ["/app/.venv/bin/python", "kokoro_server.py"]
