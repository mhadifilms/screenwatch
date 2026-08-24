"""Run the local app or the durable auditorium harvester."""

from __future__ import annotations

import argparse
import json
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screenwatch",
        description="Local theater intelligence and auditorium-map collection.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the local HTTP app")
    serve.add_argument("--db", default="screenwatch.db", help="SQLite database path")
    serve.add_argument("--host", default="127.0.0.1", help="Bind address")
    serve.add_argument("--port", type=int, default=8787, help="HTTP port")
    serve.add_argument("--reload", action="store_true", help="Enable development reload")

    harvest = commands.add_parser(
        "harvest", help="Collect durable auditorium layouts for a venue or city"
    )
    harvest.add_argument("--db", default="screenwatch.db", help="SQLite database path")
    scope = harvest.add_mutually_exclusive_group(required=True)
    scope.add_argument("--venue", help="Exact Screenwatch venue id")
    scope.add_argument("--city", help="City name from the venue directory")
    harvest.add_argument("--days", type=int, default=45, help="Collection horizon (1-90)")

    rooms = commands.add_parser("rooms", help="Show persisted rooms and layouts")
    rooms.add_argument("--db", default="screenwatch.db", help="SQLite database path")
    rooms.add_argument("--venue", required=True, help="Exact Screenwatch venue id")
    return parser


def main() -> None:
    argv = sys.argv[1:]
    # Backward compatibility: `screenwatch --port 9000` still starts the app.
    if not argv or argv[0] not in {"serve", "harvest", "rooms"}:
        argv = ["serve", *argv]
    args = _parser().parse_args(argv)

    if args.command == "serve":
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover - installation boundary
            raise SystemExit(
                "The local app needs the API extras. Install with: "
                "uv pip install -e '.[api]'"
            ) from exc

        from .api.app import app_for_db

        uvicorn.run(
            app_for_db(args.db),
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return

    from .service.defaults import default_service
    from .service.harvest import HarvestService

    search, _watches, store = default_service(args.db)
    try:
        harvest = HarvestService(search)
        if args.command == "harvest":
            result = harvest.harvest(
                venue_id=args.venue,
                city=args.city,
                days=args.days,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(json.dumps(harvest.rooms(args.venue), indent=2, sort_keys=True))
    finally:
        store.close()


if __name__ == "__main__":
    main()
