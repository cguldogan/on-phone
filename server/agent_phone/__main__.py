import logging
import os

import uvicorn

from .app import build_app
from .config import Settings


class _Redact(logging.Filter):
    """Belt and braces: never let credentials reach the logs."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(s in msg for s in self.secrets):
            for s in self.secrets:
                msg = msg.replace(s, "<redacted>")
            record.msg, record.args = msg, ()
        return True


def main() -> None:
    s = Settings.from_env()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request lines are noise
    redact = _Redact([s.mcp_bearer_token, s.ari_password])
    for h in logging.getLogger().handlers:
        h.addFilter(redact)
    # uvicorn's access log would include query strings; keep it off.
    uvicorn.run(build_app(s), host=s.http_host, port=s.http_port, access_log=False, log_level="warning",
                timeout_graceful_shutdown=3)  # SSE channel streams never end on their own


if __name__ == "__main__":
    main()
