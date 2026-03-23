from __future__ import annotations

import queue
import threading
import time
import urllib.error
import urllib.request
import os
from dataclasses import dataclass
from typing import Any

from .parser import parse_html
from .storage import Storage
from .utils import count_terms, new_job_id, normalize_url, tokenize

DEFAULT_RATE_LIMIT = 3.0
DEFAULT_WORKERS = 4
DEFAULT_QUEUE_LIMIT = 64
MAX_DOWNLOAD_BYTES = 1_500_000
USER_AGENT = "BLG483E-Homework-Crawler/1.0"


class RateLimiter:
    """Simple shared rate limiter for a crawl job.

    A job may have multiple workers, but they all acquire from the same limiter
    so the configured request rate is respected at the job level instead of per
    thread.
    """

    def __init__(self, rate_per_second: float) -> None:
        self.rate_per_second = max(rate_per_second, 0.1)
        self._next_allowed_at = 0.0
        self._lock = threading.Lock()
        self.last_wait_seconds = 0.0

    def acquire(self) -> float:
        wait_seconds = 0.0
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_at:
                wait_seconds = self._next_allowed_at - now
            scheduled_at = max(now, self._next_allowed_at) + (1.0 / self.rate_per_second)
            self._next_allowed_at = scheduled_at
            self.last_wait_seconds = wait_seconds
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        return wait_seconds


@dataclass
class FetchCoordination:
    """Result of the manager's duplicate-fetch arbitration."""

    should_fetch: bool
    event: threading.Event | None


class JobRunner:
    """Owns the active dispatcher and worker threads for one crawl job."""

    def __init__(self, manager: "CrawlerManager", job: dict[str, Any]) -> None:
        self.manager = manager
        self.storage = manager.storage
        self.job = job
        self.job_id = str(job["job_id"])
        self.origin_url = str(job["origin_url"])
        self.max_depth = int(job["max_depth"])
        self.worker_count = int(job["worker_count"])
        self.queue_limit = int(job["queue_limit"])
        self.rate_limiter = RateLimiter(float(job["rate_limit"]))
        self.work_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.queue_limit)
        self.stop_event = threading.Event()
        self.finished_event = threading.Event()
        self._active_workers = 0
        self._active_lock = threading.Lock()
        self._backpressure_events = 0
        self._last_rate_wait = 0.0
        self._workers: list[threading.Thread] = []
        self._dispatcher = threading.Thread(
            target=self._dispatcher_loop,
            name=f"dispatcher-{self.job_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.storage.log_event(
            self.job_id,
            (
                f"Job started origin={self.origin_url} depth={self.max_depth} "
                f"workers={self.worker_count} queue_limit={self.queue_limit} "
                f"rate_limit={self.rate_limiter.rate_per_second:.2f}/s"
            ),
        )
        self._dispatcher.start()
        for worker_index in range(self.worker_count):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"worker-{self.job_id}-{worker_index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def stop(self, reason: str = "Job stop requested") -> None:
        if not self.stop_event.is_set():
            self.storage.log_event(self.job_id, reason, level="warning")
            self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        self._dispatcher.join(timeout)
        for worker in self._workers:
            worker.join(timeout)

    def runtime_snapshot(self) -> dict[str, Any]:
        with self._active_lock:
            active_workers = self._active_workers
        return {
            "in_memory_queue_depth": self.work_queue.qsize(),
            "in_memory_queue_limit": self.queue_limit,
            "active_workers": active_workers,
            "backpressure_events": self._backpressure_events,
            "last_rate_wait_ms": round(self._last_rate_wait * 1000, 2),
            "finished": self.finished_event.is_set(),
        }

    def _dispatcher_loop(self) -> None:
        while not self.stop_event.is_set():
            room = self.queue_limit - self.work_queue.qsize()
            if room <= 0:
                # A full in-memory queue is our clearest back-pressure signal:
                # workers cannot keep up with discovery, so the dispatcher waits
                # instead of loading more URLs from the persisted frontier.
                self._backpressure_events += 1
                time.sleep(0.1)
                continue

            claimed_items = self.storage.claim_pending_items(self.job_id, room)
            if claimed_items:
                for item in claimed_items:
                    try:
                        self.work_queue.put_nowait(item)
                    except queue.Full:
                        self._backpressure_events += 1
                        self.storage.requeue_frontier_item(self.job_id, str(item["url"]))
                        break
                continue

            counts = self.storage.get_job_counts(self.job_id)
            with self._active_lock:
                active_workers = self._active_workers
            # Completion is declared only when disk frontier, memory queue, and
            # worker activity all reach zero simultaneously.
            if (
                counts["pending"] == 0
                and counts["queued"] == 0
                and counts["in_progress"] == 0
                and self.work_queue.empty()
                and active_workers == 0
            ):
                self.storage.update_job_status(self.job_id, "completed", completed=True)
                self.storage.log_event(self.job_id, "Job completed successfully")
                self.finished_event.set()
                return

            time.sleep(0.2)

        self.finished_event.set()

    def _worker_loop(self) -> None:
        try:
            while True:
                if self.stop_event.is_set() and self.work_queue.empty():
                    return

                try:
                    item = self.work_queue.get(timeout=0.2)
                except queue.Empty:
                    if self.finished_event.is_set():
                        return
                    continue

                url = str(item["url"])
                with self._active_lock:
                    self._active_workers += 1

                try:
                    self.storage.mark_frontier_in_progress(self.job_id, url)
                    self._process_item(item)
                except Exception as exc:  # pragma: no cover - defensive guard
                    self.storage.complete_frontier_item(self.job_id, url, "error", str(exc))
                    self.storage.log_event(self.job_id, f"Unhandled worker error for {url}: {exc}", level="error")
                finally:
                    with self._active_lock:
                        self._active_workers -= 1
                    self.work_queue.task_done()
        finally:
            self.storage.close_thread_connection()

    def _process_item(self, item: dict[str, Any]) -> None:
        url = str(item["url"])
        depth = int(item["depth"])

        if depth > self.max_depth:
            self.storage.complete_frontier_item(self.job_id, url, "skipped", "depth limit reached")
            return

        page = self.storage.get_page(url)
        if page is None:
            page = self._fetch_or_wait(url)

        if page is None:
            self.storage.complete_frontier_item(self.job_id, url, "error", "page unavailable after coordination")
            return

        self.storage.record_page_origin(int(page["page_id"]), self.job_id, self.origin_url, depth)

        if depth < self.max_depth:
            # Links were already parsed and stored with the page, so expanding
            # the frontier never has to refetch or reparse the current URL.
            links = self.storage.get_page_links(str(page["url"]))
            new_links = 0
            for link in links:
                if self.storage.add_frontier_item(self.job_id, link, depth + 1, str(page["url"])):
                    new_links += 1
            if new_links:
                self.storage.log_event(
                    self.job_id,
                    f"Discovered {new_links} link(s) from {page['url']} at depth {depth}",
                )

        self.storage.complete_frontier_item(self.job_id, url, "done")

    def _fetch_or_wait(self, url: str) -> dict[str, Any] | None:
        attempts = 0
        while attempts < 3 and not self.stop_event.is_set():
            attempts += 1
            coordination = self.manager.begin_fetch(url)
            if coordination.should_fetch:
                try:
                    return self._fetch_and_store(url)
                finally:
                    self.manager.finish_fetch(url)

            if coordination.event is None:
                return self.storage.get_page(url)

            # Another worker is already fetching the same URL. Waiting here
            # preserves the "do not crawl the same page twice" rule without
            # forcing the whole crawl to become single-threaded.
            coordination.event.wait(timeout=20)
            page = self.storage.get_page(url)
            if page is not None:
                return page

        return self.storage.get_page(url)

    def _fetch_and_store(self, url: str) -> dict[str, Any]:
        self._last_rate_wait = self.rate_limiter.acquire()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT},
        )

        final_url = url
        status_code: int | None = None
        content_type: str | None = None
        title = ""
        plain_text = ""
        links: list[str] = []
        fetch_error: str | None = None

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status_code = getattr(response, "status", 200)
                final_url = normalize_url(response.geturl()) or url
                content_type = response.headers.get_content_type()
                body = response.read(MAX_DOWNLOAD_BYTES + 1)
                # Hard-capping body size prevents one unexpectedly large page
                # from dominating crawl memory or disk in this local prototype.
                if len(body) > MAX_DOWNLOAD_BYTES:
                    body = body[:MAX_DOWNLOAD_BYTES]
                    self.storage.log_event(
                        self.job_id,
                        f"Body truncated at {MAX_DOWNLOAD_BYTES} bytes for {final_url}",
                        level="warning",
                    )

                if content_type and "html" in content_type:
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        html = body.decode(charset, errors="replace")
                    except LookupError:
                        html = body.decode("utf-8", errors="replace")
                    title, plain_text, links = parse_html(html, final_url)
                else:
                    # Non-HTML content is considered fetched, but it does not
                    # contribute searchable text or discoverable links.
                    plain_text = ""
                    links = []
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            final_url = normalize_url(exc.geturl()) or url
            content_type = exc.headers.get_content_type() if exc.headers else None
            fetch_error = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            fetch_error = f"URL error: {exc.reason}"
        except TimeoutError:
            fetch_error = "timeout"
        except Exception as exc:
            fetch_error = str(exc)

        term_counts = count_terms(plain_text)
        title_terms = set(tokenize(title))
        stored_page = self.storage.store_page_result(
            requested_url=url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            title=title,
            plain_text=plain_text[:20000],
            links=links,
            term_counts=dict(term_counts),
            title_terms=title_terms,
            fetch_error=fetch_error,
        )

        if fetch_error:
            self.storage.log_event(self.job_id, f"Fetch failed for {url}: {fetch_error}", level="warning")
        else:
            self.storage.log_event(
                self.job_id,
                f"Fetched {stored_page['url']} status={status_code} terms={len(term_counts)} links={len(links)}",
            )

        return stored_page


class CrawlerManager:
    """Process-wide coordinator for crawl jobs and live search."""

    def __init__(self, data_dir: str, auto_resume: bool = False) -> None:
        self.data_dir = data_dir
        self.storage = Storage(os.path.join(data_dir, "crawler.db"))
        self.storage.mark_jobs_resumable_on_boot()
        self._runners: dict[str, JobRunner] = {}
        self._runners_lock = threading.RLock()
        self._fetch_lock = threading.Lock()
        self._fetch_events: dict[str, threading.Event] = {}

        if auto_resume:
            for job in self.storage.resumable_jobs():
                job = self.storage.prepare_job_for_resume(str(job["job_id"]))
                self.storage.log_event(str(job["job_id"]), "Auto-resume started on process boot")
                self._start_runner(job)

    def begin_fetch(self, url: str) -> FetchCoordination:
        with self._fetch_lock:
            page = self.storage.get_page(url)
            if page is not None:
                return FetchCoordination(False, None)

            existing = self._fetch_events.get(url)
            if existing is not None:
                return FetchCoordination(False, existing)

            # The first worker to register a URL becomes the fetch owner; later
            # workers receive the event object and wait for the shared result.
            event = threading.Event()
            self._fetch_events[url] = event
            return FetchCoordination(True, event)

    def finish_fetch(self, url: str) -> None:
        with self._fetch_lock:
            event = self._fetch_events.pop(url, None)
        if event is not None:
            event.set()

    def start_job(
        self,
        origin_url: str,
        max_depth: int,
        *,
        worker_count: int = DEFAULT_WORKERS,
        rate_limit: float = DEFAULT_RATE_LIMIT,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
    ) -> dict[str, Any]:
        normalized_origin = normalize_url(origin_url)
        if normalized_origin is None:
            raise ValueError("origin_url must be a valid http/https URL")
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        if queue_limit < 1:
            raise ValueError("queue_limit must be >= 1")
        if rate_limit <= 0:
            raise ValueError("rate_limit must be > 0")

        job_id = new_job_id()
        job = self.storage.create_job(
            job_id,
            normalized_origin,
            max_depth,
            worker_count,
            rate_limit,
            queue_limit,
        )
        self.storage.add_frontier_item(job_id, normalized_origin, 0, None)
        self._start_runner(job)
        return self.get_job_status(job_id)

    def _start_runner(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        with self._runners_lock:
            existing = self._runners.get(job_id)
            if existing is not None and not existing.finished_event.is_set():
                return
            runner = JobRunner(self, job)
            self._runners[job_id] = runner
            runner.start()

    def resume_job(self, job_id: str) -> dict[str, Any]:
        job = self.storage.get_job(job_id)
        if job is None:
            raise KeyError(job_id)

        with self._runners_lock:
            existing = self._runners.get(job_id)
            if existing is not None and not existing.finished_event.is_set():
                return self.get_job_status(job_id)

        job = self.storage.prepare_job_for_resume(job_id)
        self.storage.log_event(job_id, "Job resumed from persisted frontier")
        self._start_runner(job)
        return self.get_job_status(job_id)

    def search(self, query: str, limit: int = 25) -> dict[str, Any]:
        # Query-time tokenization intentionally mirrors index-time tokenization.
        terms = tokenize(query)
        results = self.storage.search(terms, limit)
        return {
            "query": query,
            "terms": terms,
            "results": results,
            "result_count": len(results),
        }

    def search_quiz_compat(self, query: str, limit: int = 25, sort_by: str = "relevance") -> dict[str, Any]:
        """Expose the quiz-compatible query contract.

        The quiz references GET /search?query=<word>&sortBy=relevance on port
        3600 and expects the returned score to match a documented flat-file
        formula. This method keeps that compatibility separate from the richer
        dashboard API so the original homework solution remains intact.
        """
        terms = tokenize(query)
        results = self.storage.search_quiz_compat(terms, limit)
        return {
            "query": query,
            "terms": terms,
            "sortBy": sort_by,
            "results": results,
            "result_count": len(results),
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.get_job_status(str(job["job_id"])) for job in self.storage.list_jobs()]

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        job = self.storage.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        counts = self.storage.get_job_counts(job_id)
        events = self.storage.get_recent_events(job_id)
        with self._runners_lock:
            runner = self._runners.get(job_id)
        runtime = runner.runtime_snapshot() if runner is not None else {
            "in_memory_queue_depth": 0,
            "in_memory_queue_limit": int(job["queue_limit"]),
            "active_workers": 0,
            "backpressure_events": 0,
            "last_rate_wait_ms": 0.0,
            "finished": job["status"] == "completed",
        }
        return {
            **job,
            "counts": counts,
            "runtime": runtime,
            "events": events,
        }

    def system_status(self) -> dict[str, Any]:
        summary = self.storage.system_summary()
        active_jobs = []
        for job in self.storage.list_jobs():
            if str(job["status"]) in {"running", "resumable"}:
                active_jobs.append(self.get_job_status(str(job["job_id"])))
        return {
            **summary,
            "active_jobs": active_jobs,
        }

    def shutdown(self) -> None:
        # Shutdown does not simply kill worker threads. It marks unfinished
        # work resumable so the next process can continue from persisted state.
        with self._runners_lock:
            runners = list(self._runners.items())
        for job_id, runner in runners:
            runner.stop("Manager shutdown requested")
        for job_id, runner in runners:
            runner.join(timeout=2.0)
            job = self.storage.get_job(job_id)
            if job is not None and str(job["status"]) == "running":
                self.storage.mark_job_resumable(job_id, "interrupted during shutdown")
                self.storage.log_event(job_id, "Job marked resumable after shutdown", level="warning")
