"""Entry point: python -m tf2asloc.orchestrator"""

import logging
import os
import sys
from pathlib import Path

from tf2asloc.config.loader import load_config
from tf2asloc.logger.logger import setup_logging


def main() -> None:
    setup_logging(log_dir=Path("logs"))
    config_path = os.environ.get("TF2ASLOC_CONFIG", "config.yaml")
    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        logging.getLogger(__name__).error("Config not found: %s", exc)
        sys.exit(1)

    from tf2asloc.orchestrator.orchestrator import run

    run(config)


if __name__ == "__main__":
    main()
