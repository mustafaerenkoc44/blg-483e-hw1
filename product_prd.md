# Product PRD: Single-Machine Concurrent Web Crawler

## Goal

Build a localhost-runnable crawler and search system that can:

1. Start a crawl from an `origin` URL and recursively discover links until depth `k`
2. Avoid crawling the same page twice
3. Expose search results while indexing is still in progress
4. Show live system state, including queue depth and back-pressure information
5. Resume interrupted work without throwing away already indexed pages

## Primary users

- a reviewer grading the homework
- a developer running the project locally
- a user observing crawl progress and querying indexed data

## Functional requirements

### Index

- Input: `origin`, `k`
- The system must crawl pages breadth-wise by storing `(url, depth)` frontier records
- A page must not be fetched twice once it already exists in the persistent page store
- Newly discovered links must be normalized to absolute HTTP/HTTPS URLs
- Crawling stops at depth `k`
- The system must persist crawl progress during execution

### Search

- Input: `query`
- Output: list of `(relevant_url, origin_url, depth)` triples
- Search must read committed partial results while crawl jobs are still running
- A simple and explainable ranking heuristic is acceptable

### Visibility

- The UI must allow:
  - starting a crawl
  - issuing a search
  - viewing jobs and their states
  - seeing queue depth, active workers, indexed page count, and back-pressure signals

### Resume

- Interrupted jobs must become resumable
- On resume, in-flight frontier rows must return to `pending`
- Already indexed pages must not be fetched again

## Non-functional requirements

- Prefer language-native components over full crawler frameworks
- Safe for large single-machine crawls
- Thread-safe writes and reads
- Search latency should remain acceptable during indexing
- Reasonable fault tolerance for malformed pages, timeouts, redirects, and non-HTML content

## Architecture

### Core components

1. HTTP server
   - built with `http.server`
   - serves both API and dashboard

2. Crawl manager
   - creates jobs
   - owns active job runners
   - coordinates global fetch deduplication

3. Job runner
   - dispatcher thread claims pending frontier rows
   - bounded `queue.Queue` applies in-memory back pressure
   - worker threads fetch and parse pages

4. Parser
   - `html.parser` extracts text, title, and links

5. Persistent storage
   - SQLite in WAL mode
   - tables for jobs, frontier, pages, page terms, links, origins, and events

### Data flow

1. `POST /api/index` creates a job row and seeds frontier with `origin`
2. Dispatcher moves a bounded number of frontier rows into memory
3. Workers fetch pages with `urllib.request`
4. Parsed terms and links are committed to SQLite per page
5. Search queries read from the same DB while new pages keep arriving

## Relevance model

For each indexed page:

- tokenize lowercase alphanumeric words
- store term frequency per page
- mark whether each term also appeared in the title

Search score:

```text
score = 4 * frequency + 20 * title_match_bonus
sort by:
1. matched query terms desc
2. score desc
3. depth asc
4. url asc
```

## Back pressure strategy

- rate limiter enforces max requests per second
- bounded in-memory queue prevents unlimited active work buildup
- persistent frontier allows discovered URLs to wait on disk instead of consuming memory
- dashboard exposes queue occupancy and back-pressure event count

## Error handling

- malformed or unsupported URLs are ignored
- HTTP and network errors are stored as page fetch failures
- non-HTML content is marked as crawled but not tokenized
- redirects are normalized through alias mapping to avoid duplicate fetches

## Success criteria

- a crawl job can be started from the dashboard
- search returns results before the crawl fully completes
- queue depth and worker activity are visible
- restarting the process with `--auto-resume` continues unfinished jobs
- the codebase remains dependency-light and explainable

