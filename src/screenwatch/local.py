"""Launch the local Screenwatch app."""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="screenwatch",
        description="Run the local Screenwatch theater intelligence app.",
    )
    parser.add_argument("--db", default="screenwatch.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (local by default)")
    parser.add_argument("--port", type=int, default=8787, help="HTTP port")
    parser.add_argument("--reload", action="store_true", help="Enable development reload")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised by installation, not CI
        raise SystemExit(
            "The local app needs the API extras. Install with: "
            "uv pip install -e '.[api]'"
        ) from exc

    from .api.app import app_for_db

    uvicorn.run(app_for_db(args.db), host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
