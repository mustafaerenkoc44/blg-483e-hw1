from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

from .utils import utc_now_ts


class Storage:
    """SQLite-backed persistence for crawl jobs, pages, and search data.

    Storage is the main coordination point between indexing and search. Every
    important crawler state transition is persisted here so that:
    1. search can read fresh data while workers are writing, and
    2. interrupted jobs can be resumed without rebuilding the crawl from zero.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.compat_storage_dir = os.path.join(os.path.dirname(db_path), "storage")
        self.compat_storage_path = os.path.join(self.compat_storage_dir, "p.data")
        os.makedirs(self.compat_storage_dir, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_schema()
        self.refresh_compat_storage()

    def _conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            # Each thread gets its own connection. SQLite is very capable in
            # WAL mode for this single-machine use case, but sharing the same
            # connection object across threads would still be error-prone.
            connection = sqlite3.connect(
                self.db_path,
                timeout=30,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            # WAL keeps search responsive while workers commit newly fetched
            # pages. NORMAL sync is a practical latency/durability tradeoff for
            # a homework-sized local system.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self._local.connection = connection
        return connection

    def close_thread_connection(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    origin_url TEXT NOT NULL,
                    max_depth INTEGER NOT NULL,
                    worker_count INTEGER NOT NULL,
                    rate_limit REAL NOT NULL,
                    queue_limit INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS frontier (
                    job_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    parent_url TEXT,
                    state TEXT NOT NULL,
                    discovered_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    error_message TEXT,
                    PRIMARY KEY (job_id, url),
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pages (
                    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    status_code INTEGER,
                    content_type TEXT,
                    title TEXT,
                    plain_text TEXT,
                    fetched_at REAL,
                    fetch_error TEXT
                );

                CREATE TABLE IF NOT EXISTS url_aliases (
                    alias_url TEXT PRIMARY KEY,
                    page_url TEXT NOT NULL,
                    FOREIGN KEY (page_url) REFERENCES pages(url) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS page_terms (
                    page_id INTEGER NOT NULL,
                    term TEXT NOT NULL,
                    frequency INTEGER NOT NULL,
                    in_title INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (page_id, term),
                    FOREIGN KEY (page_id) REFERENCES pages(page_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS page_links (
                    page_id INTEGER NOT NULL,
                    target_url TEXT NOT NULL,
                    PRIMARY KEY (page_id, target_url),
                    FOREIGN KEY (page_id) REFERENCES pages(page_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS page_origins (
                    page_id INTEGER NOT NULL,
                    job_id TEXT NOT NULL,
                    origin_url TEXT NOT NULL,
                    discovered_depth INTEGER NOT NULL,
                    discovered_at REAL NOT NULL,
                    PRIMARY KEY (page_id, job_id),
                    FOREIGN KEY (page_id) REFERENCES pages(page_id) ON DELETE CASCADE,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_frontier_job_state
                    ON frontier(job_id, state, depth, discovered_at);
                CREATE INDEX IF NOT EXISTS idx_page_terms_term
                    ON page_terms(term);
                CREATE INDEX IF NOT EXISTS idx_page_origins_job_depth
                    ON page_origins(job_id, discovered_depth);
                CREATE INDEX IF NOT EXISTS idx_job_events_job_created
                    ON job_events(job_id, created_at DESC);
                """
            )
            conn.commit()

    def refresh_compat_storage(self) -> None:
        """Materialize the quiz-friendly raw storage file from SQLite state.

        The project's primary storage is SQLite, but the quiz expects a flat
        file at data/storage/p.data where each line has:
        word url origin depth frequency
        Exporting that file from the canonical index keeps the system
        internally consistent while still matching the quiz format exactly.
        """
        with self._write_lock:
            conn = self._conn()
            rows = conn.execute(
                """
                SELECT
                    pt.term AS word,
                    p.url AS url,
                    po.origin_url AS origin_url,
                    po.discovered_depth AS depth,
                    MAX(pt.frequency) AS frequency
                FROM page_terms pt
                JOIN pages p ON p.page_id = pt.page_id
                JOIN page_origins po ON po.page_id = pt.page_id
                GROUP BY pt.term, p.url, po.origin_url, po.discovered_depth
                ORDER BY pt.term ASC, p.url ASC, po.origin_url ASC, po.discovered_depth ASC
                """
            ).fetchall()
            with open(self.compat_storage_path, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        f"{row['word']} {row['url']} {row['origin_url']} {int(row['depth'])} {int(row['frequency'])}\n"
                    )

    def create_job(
        self,
        job_id: str,
        origin_url: str,
        max_depth: int,
        worker_count: int,
        rate_limit: float,
        queue_limit: int,
    ) -> dict[str, Any]:
        now = utc_now_ts()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, origin_url, max_depth, worker_count, rate_limit,
                    queue_limit, status, created_at, updated_at, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    job_id,
                    origin_url,
                    max_depth,
                    worker_count,
                    rate_limit,
                    queue_limit,
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_job(job_id)

    def update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        last_error: str | None = None,
        completed: bool = False,
    ) -> None:
        now = utc_now_ts()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?, completed_at = CASE
                        WHEN ? THEN ?
                        ELSE completed_at
                    END,
                    last_error = COALESCE(?, last_error)
                WHERE job_id = ?
                """,
                (status, now, 1 if completed else 0, now, last_error, job_id),
            )
            conn.commit()

    def mark_jobs_resumable_on_boot(self) -> None:
        # If the previous process died mid-crawl, any "running" job is really
        # resumable. Likewise, frontier rows that were in memory but not
        # finished must be returned to the pending pool.
        now = utc_now_ts()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                UPDATE jobs
                SET status = 'resumable', updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE frontier
                SET state = 'pending', updated_at = ?
                WHERE state IN ('queued', 'in_progress')
                """,
                (now,),
            )
            conn.commit()

    def prepare_job_for_resume(self, job_id: str) -> dict[str, Any]:
        now = utc_now_ts()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                UPDATE frontier
                SET state = 'pending', updated_at = ?
                WHERE job_id = ? AND state IN ('queued', 'in_progress')
                """,
                (now, job_id),
            )
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running', updated_at = ?, started_at = COALESCE(started_at, ?),
                    completed_at = NULL
                WHERE job_id = ?
                """,
                (now, now, job_id),
            )
            conn.commit()
        return self.get_job(job_id)

    def mark_job_resumable(self, job_id: str, reason: str | None = None) -> None:
        now = utc_now_ts()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                UPDATE frontier
                SET state = 'pending', updated_at = ?
                WHERE job_id = ? AND state IN ('queued', 'in_progress')
                """,
                (now, job_id),
            )
            conn.execute(
                """
                UPDATE jobs
                SET status = 'resumable', updated_at = ?, last_error = COALESCE(?, last_error)
                WHERE job_id = ?
                """,
                (now, reason, job_id),
            )
            conn.commit()

    def log_event(self, job_id: str, message: str, level: str = "info") -> None:
        now = utc_now_ts()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT INTO job_events (job_id, created_at, level, message)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, now, level, message),
            )
            conn.execute(
                "UPDATE jobs SET updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            conn.commit()

    def add_frontier_item(
        self,
        job_id: str,
        url: str,
        depth: int,
        parent_url: str | None,
    ) -> bool:
        """Insert or improve a frontier record for a specific job.

        The method returns True only when the frontier meaningfully changes,
        which lets callers count newly discovered work without re-counting URLs
        already known at the same or better depth.
        """
        now = utc_now_ts()
        with self._write_lock:
            conn = self._conn()
            existing = conn.execute(
                "SELECT depth, state FROM frontier WHERE job_id = ? AND url = ?",
                (job_id, url),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO frontier (
                        job_id, url, depth, parent_url, state, discovered_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (job_id, url, depth, parent_url, now, now),
                )
                conn.commit()
                return True

            if depth < int(existing["depth"]):
                new_state = "pending" if existing["state"] in {"error", "skipped"} else existing["state"]
                conn.execute(
                    """
                    UPDATE frontier
                    SET depth = ?, parent_url = COALESCE(parent_url, ?), state = ?, updated_at = ?
                    WHERE job_id = ? AND url = ?
                    """,
                    (depth, parent_url, new_state, now, job_id, url),
                )
                conn.commit()
                return True

            return False

    def claim_pending_items(self, job_id: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        with self._write_lock:
            conn = self._conn()
            # Frontier is ordered breadth-first by depth, then by discovery time.
            # That keeps traversal behavior aligned with the assignment's depth
            # semantics while still allowing multiple workers to process pages.
            rows = conn.execute(
                """
                SELECT url, depth, parent_url
                FROM frontier
                WHERE job_id = ? AND state = 'pending'
                ORDER BY depth ASC, discovered_at ASC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
            if not rows:
                return []

            now = utc_now_ts()
            conn.executemany(
                """
                UPDATE frontier
                SET state = 'queued', updated_at = ?
                WHERE job_id = ? AND url = ?
                """,
                [(now, job_id, row["url"]) for row in rows],
            )
            conn.commit()

        return [dict(row) for row in rows]

    def mark_frontier_in_progress(self, job_id: str, url: str) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                UPDATE frontier
                SET state = 'in_progress', updated_at = ?
                WHERE job_id = ? AND url = ?
                """,
                (utc_now_ts(), job_id, url),
            )
            conn.commit()

    def complete_frontier_item(
        self,
        job_id: str,
        url: str,
        state: str,
        error_message: str | None = None,
    ) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                UPDATE frontier
                SET state = ?, error_message = ?, updated_at = ?
                WHERE job_id = ? AND url = ?
                """,
                (state, error_message, utc_now_ts(), job_id, url),
            )
            conn.commit()

    def requeue_frontier_item(self, job_id: str, url: str) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                """
                UPDATE frontier
                SET state = 'pending', updated_at = ?
                WHERE job_id = ? AND url = ?
                """,
                (utc_now_ts(), job_id, url),
            )
            conn.commit()

    def _resolve_page_url(self, url: str) -> str | None:
        # Redirect targets are stored in pages, while the originally requested
        # URL may be stored as an alias. Search and crawl callers should not
        # need to know which one they are holding.
        conn = self._conn()
        row = conn.execute(
            "SELECT url FROM pages WHERE url = ?",
            (url,),
        ).fetchone()
        if row:
            return str(row["url"])
        alias_row = conn.execute(
            "SELECT page_url FROM url_aliases WHERE alias_url = ?",
            (url,),
        ).fetchone()
        return None if alias_row is None else str(alias_row["page_url"])

    def get_page(self, url: str) -> dict[str, Any] | None:
        resolved_url = self._resolve_page_url(url)
        if resolved_url is None:
            return None
        row = self._conn().execute(
            """
            SELECT page_id, url, status_code, content_type, title, plain_text, fetched_at, fetch_error
            FROM pages
            WHERE url = ?
            """,
            (resolved_url,),
        ).fetchone()
        return None if row is None else dict(row)

    def store_page_result(
        self,
        requested_url: str,
        final_url: str,
        status_code: int | None,
        content_type: str | None,
        title: str,
        plain_text: str,
        links: list[str],
        term_counts: dict[str, int],
        title_terms: set[str],
        fetch_error: str | None = None,
    ) -> dict[str, Any]:
        """Persist a fetched page and its inverted-index representation.

        Pages are stored globally by canonical URL, not by job. Job-specific
        discovery information is recorded separately in page_origins so the same
        page can later be returned for multiple crawl runs with different
        (origin, depth) metadata.
        """
        with self._write_lock:
            conn = self._conn()
            existing = self.get_page(final_url)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO pages (
                        url, status_code, content_type, title, plain_text, fetched_at, fetch_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        final_url,
                        status_code,
                        content_type,
                        title,
                        plain_text,
                        utc_now_ts(),
                        fetch_error,
                    ),
                )
                page_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                if term_counts:
                    # page_terms is the minimal inverted index used by /search.
                    conn.executemany(
                        """
                        INSERT INTO page_terms (page_id, term, frequency, in_title)
                        VALUES (?, ?, ?, ?)
                        """,
                        [
                            (page_id, term, frequency, 1 if term in title_terms else 0)
                            for term, frequency in term_counts.items()
                        ],
                    )
                if links:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO page_links (page_id, target_url)
                        VALUES (?, ?)
                        """,
                        [(page_id, link) for link in links],
                    )
            else:
                page_id = int(existing["page_id"])

            if requested_url != final_url:
                # Redirects are collapsed into one page row, but the originally
                # requested URL is still useful as a lookup alias.
                conn.execute(
                    """
                    INSERT INTO url_aliases (alias_url, page_url)
                    VALUES (?, ?)
                    ON CONFLICT(alias_url) DO UPDATE SET page_url = excluded.page_url
                    """,
                    (requested_url, final_url),
                )

            conn.commit()

        page = self.get_page(final_url)
        if page is None:
            raise RuntimeError("page insert unexpectedly missing")
        return page

    def record_page_origin(
        self,
        page_id: int,
        job_id: str,
        origin_url: str,
        depth: int,
    ) -> None:
        # The same canonical page may be reachable from many crawl jobs or from
        # different depths. This table is what allows search to return the
        # assignment's required triple: (relevant_url, origin_url, depth).
        with self._write_lock:
            conn = self._conn()
            existing = conn.execute(
                """
                SELECT discovered_depth
                FROM page_origins
                WHERE page_id = ? AND job_id = ?
                """,
                (page_id, job_id),
            ).fetchone()
            now = utc_now_ts()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO page_origins (
                        page_id, job_id, origin_url, discovered_depth, discovered_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (page_id, job_id, origin_url, depth, now),
                )
            elif depth < int(existing["discovered_depth"]):
                conn.execute(
                    """
                    UPDATE page_origins
                    SET discovered_depth = ?, discovered_at = ?
                    WHERE page_id = ? AND job_id = ?
                    """,
                        (depth, now, page_id, job_id),
                )
            conn.commit()
            self.refresh_compat_storage()

    def get_page_links(self, url: str) -> list[str]:
        page = self.get_page(url)
        if page is None:
            return []
        rows = self._conn().execute(
            """
            SELECT target_url
            FROM page_links
            WHERE page_id = ?
            ORDER BY target_url ASC
            """,
            (page["page_id"],),
        ).fetchall()
        return [str(row["target_url"]) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            """
            SELECT job_id, origin_url, max_depth, worker_count, rate_limit, queue_limit,
                   status, created_at, updated_at, started_at, completed_at, last_error
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def list_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT job_id, origin_url, max_depth, worker_count, rate_limit, queue_limit,
                   status, created_at, updated_at, started_at, completed_at, last_error
            FROM jobs
            ORDER BY created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_events(self, job_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT created_at, level, message
            FROM job_events
            WHERE job_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (job_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_job_counts(self, job_id: str) -> dict[str, int]:
        counts = {
            "pending": 0,
            "queued": 0,
            "in_progress": 0,
            "done": 0,
            "error": 0,
            "skipped": 0,
            "indexed_pages": 0,
        }

        frontier_rows = self._conn().execute(
            """
            SELECT state, COUNT(*) AS count
            FROM frontier
            WHERE job_id = ?
            GROUP BY state
            """,
            (job_id,),
        ).fetchall()
        for row in frontier_rows:
            counts[str(row["state"])] = int(row["count"])

        indexed_row = self._conn().execute(
            """
            SELECT COUNT(*) AS count
            FROM page_origins
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        counts["indexed_pages"] = 0 if indexed_row is None else int(indexed_row["count"])
        return counts

    def search(self, query_terms: list[str], limit: int) -> list[dict[str, Any]]:
        if not query_terms:
            return []

        placeholders = ", ".join("?" for _ in query_terms)
        # Grouping by relevant_url + origin_url + depth avoids duplicate rows
        # when the same page has been observed by multiple jobs with identical
        # crawl context. The score remains a simple frequency/title heuristic.
        sql = f"""
            SELECT
                p.url AS relevant_url,
                p.title AS title,
                po.origin_url AS origin_url,
                po.discovered_depth AS depth,
                SUM(pt.frequency * 4 + CASE WHEN pt.in_title = 1 THEN 20 ELSE 0 END) AS score,
                COUNT(DISTINCT pt.term) AS matched_terms
            FROM page_terms pt
            JOIN pages p ON p.page_id = pt.page_id
            JOIN page_origins po ON po.page_id = pt.page_id
            WHERE pt.term IN ({placeholders})
            GROUP BY p.url, p.title, po.origin_url, po.discovered_depth
            ORDER BY matched_terms DESC, score DESC, depth ASC, relevant_url ASC
            LIMIT ?
        """
        rows = self._conn().execute(sql, (*query_terms, limit)).fetchall()
        return [dict(row) for row in rows]

    def search_quiz_compat(self, query_terms: list[str], limit: int) -> list[dict[str, Any]]:
        """Return results using the quiz's explicit relevance formula.

        Quiz formula:
            score = (frequency * 10) + 1000 - (depth * 5)

        This endpoint intentionally exposes frequency and depth directly so the
        quiz answer can be audited against raw p.data lines.
        """
        if not query_terms:
            return []

        placeholders = ", ".join("?" for _ in query_terms)
        exact_bonus = 1000
        sql = f"""
            WITH deduped_matches AS (
                SELECT
                    pt.term AS term,
                    p.url AS url,
                    po.origin_url AS origin,
                    po.discovered_depth AS depth,
                    MAX(pt.frequency) AS frequency
                FROM page_terms pt
                JOIN pages p ON p.page_id = pt.page_id
                JOIN page_origins po ON po.page_id = pt.page_id
                WHERE pt.term IN ({placeholders})
                GROUP BY pt.term, p.url, po.origin_url, po.discovered_depth
            )
            SELECT
                url,
                origin,
                depth,
                SUM(frequency) AS frequency,
                SUM(frequency * 10 + {exact_bonus}) - (depth * 5) AS relevance_score,
                COUNT(DISTINCT term) AS matched_terms
            FROM deduped_matches
            GROUP BY url, origin, depth
            ORDER BY matched_terms DESC, relevance_score DESC, url ASC, origin ASC
            LIMIT ?
        """
        rows = self._conn().execute(sql, (*query_terms, limit)).fetchall()
        return [dict(row) for row in rows]

    def system_summary(self) -> dict[str, Any]:
        """Return the lightweight metrics needed by the dashboard header."""
        conn = self._conn()
        jobs_total = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        pages_total = int(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
        frontier_total = int(conn.execute("SELECT COUNT(*) FROM frontier").fetchone()[0])
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ).fetchall()
        jobs_by_status = {str(row["status"]): int(row["count"]) for row in status_rows}
        return {
            "jobs_total": jobs_total,
            "pages_total": pages_total,
            "frontier_total": frontier_total,
            "jobs_by_status": jobs_by_status,
        }

    def resumable_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT job_id, origin_url, max_depth, worker_count, rate_limit, queue_limit,
                   status, created_at, updated_at, started_at, completed_at, last_error
            FROM jobs
            WHERE status = 'resumable'
            ORDER BY created_at ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
