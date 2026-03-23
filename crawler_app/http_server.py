from __future__ import annotations

import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .manager import CrawlerManager


class CrawlerHTTPServer(ThreadingHTTPServer):
    """HTTP server that bundles the dashboard and JSON API in one process."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        manager: CrawlerManager,
        static_dir: str,
    ) -> None:
        super().__init__(server_address, request_handler_class)
        self.manager = manager
        self.static_dir = static_dir


class CrawlerRequestHandler(BaseHTTPRequestHandler):
    server: CrawlerHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        # The handler keeps routing explicit on purpose. For this homework a
        # tiny handwritten router is easier to audit than pulling in a web
        # framework, and it keeps the stdlib-first design intact.
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_static("index.html")
            return

        if path.startswith("/static/"):
            self._serve_static(path.removeprefix("/static/"))
            return

        if path == "/api/status":
            self._send_json(self.server.manager.system_status())
            return

        if path == "/api/jobs":
            self._send_json({"jobs": self.server.manager.list_jobs()})
            return

        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/")
            if "/" in job_id:
                self._send_json({"error": "unknown endpoint"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                self._send_json(self.server.manager.get_job_status(job_id))
            except KeyError:
                self._send_json({"error": "job not found"}, status=HTTPStatus.NOT_FOUND)
            return

        if path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            limit = int(params.get("limit", ["25"])[0])
            self._send_json(self.server.manager.search(query, limit=limit))
            return

        if path == "/search":
            params = parse_qs(parsed.query)
            query = params.get("query", params.get("q", [""]))[0]
            limit = int(params.get("limit", ["25"])[0])
            sort_by = params.get("sortBy", ["relevance"])[0]
            self._send_json(self.server.manager.search_quiz_compat(query, limit=limit, sort_by=sort_by))
            return

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/index":
            try:
                # The API translates user-facing HTTP input directly into a
                # crawl job. Validation lives in the manager so the same rules
                # apply even if the system later grows a CLI or another client.
                payload = self._read_json_body()
                origin = str(payload.get("origin", "")).strip()
                max_depth = int(payload.get("max_depth", 0))
                worker_count = int(payload.get("worker_count", 4))
                rate_limit = float(payload.get("rate_limit", 3.0))
                queue_limit = int(payload.get("queue_limit", 64))
                job = self.server.manager.start_job(
                    origin,
                    max_depth,
                    worker_count=worker_count,
                    rate_limit=rate_limit,
                    queue_limit=queue_limit,
                )
                self._send_json(job, status=HTTPStatus.ACCEPTED)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except json.JSONDecodeError:
                self._send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
            return

        if path.startswith("/api/jobs/") and path.endswith("/resume"):
            job_id = path[len("/api/jobs/") : -len("/resume")].strip("/")
            try:
                job = self.server.manager.resume_job(job_id)
                self._send_json(job, status=HTTPStatus.ACCEPTED)
            except KeyError:
                self._send_json({"error": "job not found"}, status=HTTPStatus.NOT_FOUND)
            return

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(body.decode("utf-8"))

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, relative_path: str) -> None:
        relative_path = relative_path.replace("\\", "/").lstrip("/")
        full_path = os.path.abspath(os.path.join(self.server.static_dir, relative_path))
        static_root = os.path.abspath(self.server.static_dir)
        # Guard against directory traversal by ensuring the resolved path stays
        # inside the static root before attempting to read a file.
        if not full_path.startswith(static_root) or not os.path.isfile(full_path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        with open(full_path, "rb") as handle:
            content = handle.read()

        content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def build_server(host: str, port: int, manager: CrawlerManager, static_dir: str) -> CrawlerHTTPServer:
    """Construct the combined dashboard/API server."""
    return CrawlerHTTPServer((host, port), CrawlerRequestHandler, manager, static_dir)
