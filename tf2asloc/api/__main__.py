"""Entry point: python -m tf2asloc.api"""

import os

import uvicorn


def main() -> None:
    config_path = os.environ.get("TF2ASLOC_CONFIG", "config.yaml")
    # Import here so create_app() picks up the env var
    from tf2asloc.api.app import create_app

    app = create_app(config_path)

    import yaml

    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    api_cfg = cfg.get("api", {})
    uvicorn.run(
        app,
        host=api_cfg.get("host", "0.0.0.0"),
        port=api_cfg.get("port", 8000),
    )


if __name__ == "__main__":
    main()
