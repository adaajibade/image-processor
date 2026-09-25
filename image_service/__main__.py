import logging

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    config = Config.from_env()
    level = "critical" if config.log_level == "silent" else config.log_level
    logging.basicConfig(level=level.upper(), format="%(message)s")
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level=level,
        access_log=False,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=25,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
