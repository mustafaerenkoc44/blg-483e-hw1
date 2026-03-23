from __future__ import annotations

import argparse
import os

from crawler_app.http_server import build_server
from crawler_app.manager import CrawlerManager


def parse_args() -> argparse.Namespace:
    """Parse the small set of runtime flags exposed by the local server."""
    parser = argparse.ArgumentParser(description="Localhost crawler homework solution")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host to bind")
    parser.add_argument("--port", default=3600, type=int, help="HTTP port to bind")
    parser.add_argument(
        "--data-dir",
        default=os.path.join(os.getcwd(), "data"),
        help="Directory used for SQLite persistence",
    )
    parser.add_argument(
        "--auto-resume",
        action="store_true",
        help="Automatically resume resumable jobs on startup",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    # The dashboard is served from the workspace so the project stays entirely
    # self-contained and runnable on localhost.
    static_dir = os.path.join(os.getcwd(), "static")
    manager = CrawlerManager(args.data_dir, auto_resume=args.auto_resume)
    server = build_server(args.host, args.port, manager, static_dir)
    print(f"Serving crawler UI at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.shutdown()


if __name__ == "__main__":
    main()
