from __future__ import annotations

import uvicorn

from agent.api.http_app import create_app
from agent.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.http_bind,
        port=settings.http_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
