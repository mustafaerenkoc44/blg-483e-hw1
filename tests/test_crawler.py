from __future__ import annotations

import functools
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from crawler_app.manager import CrawlerManager


class DelayedFileHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str, delays: dict[str, float], **kwargs):
        self._delays = delays
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self):  # noqa: N802
        delay = self._delays.get(self.path, 0)
        if delay:
            time.sleep(delay)
        super().do_GET()

    def log_message(self, format, *args):
        return


def start_site(directory: str, delays: dict[str, float] | None = None):
    handler = functools.partial(DelayedFileHandler, directory=directory, delays=delays or {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class CrawlerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdirs: list[str] = []
        self.managers: list[CrawlerManager] = []
        self.servers: list[ThreadingHTTPServer] = []
        self.workspace_tmp = Path(os.getcwd()) / "tests" / ".tmp"
        self.workspace_tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for tempdir in self.tempdirs:
            shutil.rmtree(tempdir, ignore_errors=True)

    def _tempdir(self) -> str:
        tempdir = str(self.workspace_tmp / f"case_{uuid.uuid4().hex}")
        Path(tempdir).mkdir(parents=True, exist_ok=False)
        self.tempdirs.append(tempdir)
        return tempdir

    def _make_site(self, *, slow_seconds: float = 0.0) -> str:
        root = Path(self._tempdir())
        (root / "index.html").write_text(
            """
            <html>
              <head><title>Root Page</title></head>
              <body>
                rootterm concurrency queue relevance
                <a href="/page-a.html">A</a>
                <a href="/slow.html">Slow</a>
              </body>
            </html>
            """,
            encoding="utf-8",
        )
        (root / "page-a.html").write_text(
            """
            <html>
              <head><title>Python Crawler</title></head>
              <body>
                python urllib sqlite parser ranking relevance
              </body>
            </html>
            """,
            encoding="utf-8",
        )
        (root / "slow.html").write_text(
            """
            <html>
              <head><title>Delayed Relevance</title></head>
              <body>
                delayedtoken relevance depth visibility relevance
              </body>
            </html>
            """,
            encoding="utf-8",
        )

        server, _ = start_site(str(root), delays={"/slow.html": slow_seconds})
        self.servers.append(server)
        return f"http://127.0.0.1:{server.server_port}/index.html"

    def _manager(self, data_dir: str, *, auto_resume: bool = False) -> CrawlerManager:
        manager = CrawlerManager(data_dir, auto_resume=auto_resume)
        self.managers.append(manager)
        return manager

    def test_crawl_completes_and_searches_indexed_terms(self):
        origin = self._make_site()
        data_dir = self._tempdir()
        manager = self._manager(data_dir)
        job = manager.start_job(origin, 2, worker_count=2, rate_limit=20, queue_limit=8)
        job_id = job["job_id"]

        finished = wait_until(
            lambda: manager.get_job_status(job_id)["status"] == "completed",
            timeout=8,
        )
        self.assertTrue(finished, "crawl job did not complete in time")

        results = manager.search("python", limit=10)["results"]
        urls = {row["relevant_url"] for row in results}
        self.assertIn(origin.replace("index.html", "page-a.html"), urls)

        page_a = next(row for row in results if row["relevant_url"].endswith("/page-a.html"))
        self.assertEqual(page_a["origin_url"], origin)
        self.assertEqual(page_a["depth"], 1)

    def test_search_returns_results_while_indexing_is_still_running(self):
        origin = self._make_site(slow_seconds=2.5)
        data_dir = self._tempdir()
        manager = self._manager(data_dir)
        job = manager.start_job(origin, 2, worker_count=1, rate_limit=20, queue_limit=4)
        job_id = job["job_id"]

        saw_live_result = wait_until(
            lambda: manager.get_job_status(job_id)["status"] == "running"
            and manager.search("rootterm", limit=10)["result_count"] > 0,
            timeout=4,
        )
        self.assertTrue(saw_live_result, "search never surfaced partial crawl results")

    def test_jobs_are_resumable_after_interruption(self):
        origin = self._make_site(slow_seconds=3.0)
        data_dir = self._tempdir()
        manager = self._manager(data_dir)
        job = manager.start_job(origin, 2, worker_count=1, rate_limit=20, queue_limit=4)
        job_id = job["job_id"]

        time.sleep(0.4)
        manager.shutdown()
        self.managers.remove(manager)
        time.sleep(3.2)

        resumed_manager = self._manager(data_dir, auto_resume=True)

        finished = wait_until(
            lambda: resumed_manager.get_job_status(job_id)["status"] == "completed",
            timeout=8,
        )
        self.assertTrue(finished, "auto-resumed job did not complete")

        resumed_results = resumed_manager.search("delayedtoken", limit=10)["results"]
        self.assertTrue(any(row["relevant_url"].endswith("/slow.html") for row in resumed_results))

    def test_quiz_compat_export_and_relevance_formula(self):
        origin = self._make_site()
        data_dir = self._tempdir()
        manager = self._manager(data_dir)
        job = manager.start_job(origin, 2, worker_count=2, rate_limit=20, queue_limit=8)
        job_id = job["job_id"]

        finished = wait_until(
            lambda: manager.get_job_status(job_id)["status"] == "completed",
            timeout=8,
        )
        self.assertTrue(finished, "crawl job did not complete in time")

        compat_file = Path(data_dir) / "storage" / "p.data"
        self.assertTrue(compat_file.exists(), "compat storage file was not exported")

        relevance_lines = [
            line.strip()
            for line in compat_file.read_text(encoding="utf-8").splitlines()
            if line.startswith("relevance ")
        ]
        self.assertEqual(len(relevance_lines), 3)

        compat_results = manager.search_quiz_compat("relevance", limit=10)["results"]
        self.assertEqual(compat_results[0]["frequency"], 3)
        self.assertEqual(compat_results[0]["depth"], 1)
        self.assertEqual(compat_results[0]["relevance_score"], 1025)

    def test_quiz_compat_dedupes_repeated_crawls(self):
        origin = self._make_site()
        data_dir = self._tempdir()
        manager = self._manager(data_dir)

        first_job = manager.start_job(origin, 2, worker_count=2, rate_limit=20, queue_limit=8)
        second_job = manager.start_job(origin, 2, worker_count=2, rate_limit=20, queue_limit=8)

        self.assertTrue(
            wait_until(lambda: manager.get_job_status(first_job["job_id"])["status"] == "completed", timeout=8)
        )
        self.assertTrue(
            wait_until(lambda: manager.get_job_status(second_job["job_id"])["status"] == "completed", timeout=8)
        )

        compat_results = manager.search_quiz_compat("relevance", limit=10)["results"]
        top_result = compat_results[0]
        self.assertEqual(top_result["url"].split("/")[-1], "slow.html")
        self.assertEqual(top_result["frequency"], 3)
        self.assertEqual(top_result["relevance_score"], 1025)


if __name__ == "__main__":
    unittest.main()
