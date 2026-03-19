from __future__ import annotations

from html.parser import HTMLParser

from .utils import normalize_url


class LinkTextParser(HTMLParser):
    """Very small native HTML parser for text, title, and links.

    The assignment explicitly discourages relying on full crawler libraries.
    This parser only extracts what the indexer needs and intentionally ignores
    richer HTML semantics such as DOM reconstruction or CSS/script execution.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[str] = []
        self.text_chunks: list[str] = []
        self.title_chunks: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower_tag = tag.lower()
        # Script/style/noscript blocks are skipped entirely because they do not
        # represent user-visible page text for our search index.
        if lower_tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return

        if lower_tag == "title":
            self._in_title = True
            return

        if lower_tag != "a":
            return

        for attr_name, attr_value in attrs:
            if attr_name.lower() == "href" and attr_value:
                url = normalize_url(attr_value, self.base_url)
                if url:
                    self.links.append(url)

    def handle_endtag(self, tag: str) -> None:
        lower_tag = tag.lower()
        if lower_tag in {"script", "style", "noscript"} and self._ignored_depth > 0:
            self._ignored_depth -= 1
        elif lower_tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        self.text_chunks.append(cleaned)
        if self._in_title:
            self.title_chunks.append(cleaned)


def parse_html(html: str, base_url: str) -> tuple[str, str, list[str]]:
    """Parse HTML into the three pieces the crawler indexes.

    Returning deduplicated links here keeps downstream crawl logic simpler:
    frontier expansion can focus on depth and duplicate policy instead of HTML
    parsing quirks.
    """
    parser = LinkTextParser(base_url)
    parser.feed(html)
    title = " ".join(parser.title_chunks).strip()
    text = " ".join(parser.text_chunks).strip()
    links = list(dict.fromkeys(parser.links))
    return title, text, links
