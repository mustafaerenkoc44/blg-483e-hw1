from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from posixpath import normpath
from urllib.parse import urljoin, urlsplit, urlunsplit

# We intentionally keep tokenization simple and deterministic so that indexing
# and querying use exactly the same normalization rules.
TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")


def utc_now_ts() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def new_job_id() -> str:
    return f"job_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def count_terms(text: str) -> Counter[str]:
    return Counter(tokenize(text))


def normalize_url(raw_url: str, base_url: str | None = None) -> str | None:
    """Normalize a candidate URL into a stable crawl key.

    The crawler uses this function everywhere a URL crosses a system boundary:
    user input, parsed links, redirect targets, and database lookups. Keeping
    normalization in one place is what makes "do not crawl the same page twice"
    enforceable across jobs.
    """
    if not raw_url:
        return None

    joined = urljoin(base_url or "", raw_url.strip())
    parts = urlsplit(joined)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None

    hostname = (parts.hostname or "").lower()
    if not hostname:
        return None

    port = parts.port
    default_port = 80 if parts.scheme.lower() == "http" else 443
    # Preserve only non-default ports so equivalent URLs collapse to one key.
    if port and port != default_port:
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = parts.path or "/"
    # normpath removes duplicate separators and collapses "." / ".." segments.
    normalized_path = normpath(path)
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path = f"{normalized_path}/"
    if normalized_path == "//":
        normalized_path = "/"

    return urlunsplit(
        (
            parts.scheme.lower(),
            netloc,
            normalized_path,
            parts.query,
            "",
        )
    )
