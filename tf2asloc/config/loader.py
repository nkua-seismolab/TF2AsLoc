import os
from pathlib import Path
from urllib.parse import quote_plus

import yaml

_REQUIRED_DB_ENV_VARS = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")


def _build_database_url(db_cfg: dict) -> str:
    """Build the PostgreSQL URL from POSTGRES_* env vars plus host/port from config."""
    missing = [name for name in _REQUIRED_DB_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise ValueError(
            f"Missing required database environment variable(s): {', '.join(missing)}. "
            "Set them in .env (see .env.example)."
        )
    user = quote_plus(os.environ["POSTGRES_USER"])
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    dbname = os.environ["POSTGRES_DB"]
    host = db_cfg.get("host", "localhost")
    port = db_cfg.get("port", 5432)
    # Explicitly state +psycopg2, as SQLAlchemy >= 2.1
    # resolves bare postgresql:// to psycopg3.
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"


def load_config(path: str) -> dict:
    """Load configuration from a YAML file and return it as a plain dict."""
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open("r") as fh:
        cfg = yaml.safe_load(fh)
    if cfg is None:
        raise ValueError(f"Config file is empty: {config_path}")
    # Metadata directory sits next to the config file.
    # Users supply bare filenames in config; the full path is resolved here.
    cfg["_metadata_dir"] = str(config_path.parent / "metadata")
    cfg.setdefault("database", {})["url"] = _build_database_url(cfg.get("database", {}))
    return cfg
