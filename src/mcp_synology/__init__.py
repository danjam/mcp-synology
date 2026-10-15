"""MCP server for Synology DSM."""

import atexit
import logging
import sys


def main() -> None:
    from .client import Nas
    from .config import Settings
    from .server import build

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    nas = Nas(settings)
    atexit.register(nas.close)
    build(settings, nas).run("stdio")
