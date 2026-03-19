# BLG 483E HW1: Localhost Crawler

This repository contains a single-machine web crawler and live search engine built almost entirely with Python standard library components. It satisfies the assignment requirements with:

- `index(origin, k)` semantics through a bounded, persisted frontier queue
- live `search(query)` over already indexed content while crawling is still active
- queue-depth and rate-limit back pressure
- a localhost web UI for starting jobs, searching, and observing runtime state
- resumable crawl jobs backed by SQLite

## Why this design

The assignment explicitly asks for scalable single-machine behavior, thread-safe indexing, and native functionality instead of high-level crawler libraries. Because of that, the implementation uses:

- `urllib.request` for fetching pages
- `html.parser` for extracting text and links
- `sqlite3` in WAL mode for persistent indexing and concurrent reads during writes
- `http.server` for the UI/API server
- `threading` and `queue.Queue` for worker coordination and bounded in-memory work queues

## Project structure

```text
.
├── crawler_app/
│   ├── http_server.py
│   ├── manager.py
│   ├── parser.py
│   ├── storage.py
│   └── utils.py
├── sample_site/
├── static/
├── tests/
├── main.py
├── product_prd.md
├── recommendation.md
└── README.md
```

## Run locally

Python 3.12 is enough. No external dependencies are required.

1. Start a tiny demo website in one terminal:

```bash
python -m http.server 9001 -d sample_site
```

2. Start the crawler dashboard in another terminal:

```bash
python main.py --host 127.0.0.1 --port 8080 --auto-resume
```

3. Open:

```text
http://127.0.0.1:8080
```

4. Create a crawl job with:

```text
Origin URL: http://127.0.0.1:9001/index.html
Max Depth: 2
Workers: 4
Rate Limit: 3
Queue Limit: 64
```

5. Search for:

```text
python
relevance
concurrency
```

## How it works

### Indexing

Each crawl job persists discovered URLs into a `frontier` table. A dispatcher moves only a bounded number of rows into an in-memory `queue.Queue`, which acts as the main back-pressure boundary. Worker threads pull from this queue, fetch pages, tokenize text, store an inverted index in SQLite, and expand outgoing links until `depth == k`.

Global URL deduplication is enforced through the persistent `pages` table plus an in-memory fetch coordinator. If two jobs reach the same page at once, only one thread fetches it; the others wait and then reuse the stored page and links. This avoids duplicate crawling without sacrificing concurrent search visibility.

### Search

Search tokenizes the query and runs direct SQL lookups over the inverted index (`page_terms`). Results are joined with `page_origins`, so the response naturally returns the required triples:

- `relevant_url`
- `origin_url`
- `depth`

The ranking heuristic is intentionally simple and explicit:

- higher term frequency scores higher
- title hits get an extra bonus
- shallower pages are preferred

Because SQLite runs in WAL mode and each page commit is transactional, search can read fresh results while indexing is still writing new ones.

### Resume after interruption

On startup the process marks unfinished jobs as `resumable`. If `--auto-resume` is passed, those jobs are restarted from the persisted frontier. Any rows that were `queued` or `in_progress` are reset back to `pending` so the crawl continues from where it left off instead of restarting from scratch.

## HTTP API

### Start crawl

```http
POST /api/index
Content-Type: application/json

{
  "origin": "http://127.0.0.1:9001/index.html",
  "max_depth": 2,
  "worker_count": 4,
  "rate_limit": 3.0,
  "queue_limit": 64
}
```

### Search

```http
GET /api/search?q=python&limit=20
```

### System status

```http
GET /api/status
GET /api/jobs
GET /api/jobs/{job_id}
POST /api/jobs/{job_id}/resume
```

## Testing

Run:

```bash
python -m unittest discover -s tests -v
```

The tests spin up a local HTTP server, crawl a miniature website, verify indexed search results, and verify that interrupted jobs are resumable.

## Notes on assignment interpretation

- “Never crawl the same page twice” is implemented globally within one running system and across persisted data, not just within a single job.
- “Search must run while indexing is active” is implemented with transactional page-by-page commits rather than end-of-job batch indexing.
- “Back pressure” is implemented through both rate limiting and bounded in-memory queue depth.

