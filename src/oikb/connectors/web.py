"""Web connector — crawl a website or sitemap and sync pages to a Knowledge Base.

Requires: pip install oikb[web]
Uses sitemap.xml for discovery or same-domain link crawling.
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


def _html_to_text(html: str) -> str:
    """Extract text from HTML, stripping tags."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        # Remove script and style elements.
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        # Fallback: regex strip.
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()


class WebConnector(BaseConnector):
    """Crawl a website and produce a manifest of pages.

    Args:
        url:       Root URL or sitemap URL.
        delay:     Delay between requests in seconds (default: 0.5).
        max_pages: Maximum number of pages to crawl (default: 500).
    """

    def __init__(
        self,
        url: str,
        delay: float = 0.5,
        max_pages: int = 500,
    ):
        self.url = url.rstrip("/")
        self.delay = delay
        self.max_pages = max_pages
        self._parsed = urlparse(self.url)
        self._domain = f"{self._parsed.scheme}://{self._parsed.netloc}"

        self._http = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "oikb/0.1 (+https://github.com/open-webui/oikb)"},
        )

        # Cache: url -> text content.
        self._cache: dict[str, str] = {}
        self._files: dict[tuple[str, str], str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        """Discover pages via sitemap or crawling, then build manifest."""
        urls = self._discover_urls()
        self._files.clear()
        files_by_name: dict[tuple[str, str], list[tuple[bool, str]]] = {}
        entries: list[ManifestEntry] = []

        for url in dict.fromkeys(urls[:self.max_pages]):
            try:
                text = self._fetch_page(url)
                if not text.strip():
                    continue

                self._cache[url] = text

                # Convert URL to a filename.
                path_part = urlparse(url).path.strip("/")
                if not path_part:
                    path_part = "index"

                parts = path_part.rsplit("/", 1)
                if len(parts) == 2:
                    dir_path, name = parts
                else:
                    dir_path, name = "", parts[0]

                raw_name = name
                name = re.sub(r"[^\w\-.]", "_", raw_name)
                if not name.endswith(".txt"):
                    name += ".txt"

                lookup_changed = raw_name != name.removesuffix(".txt")
                files_by_name.setdefault((dir_path, name), []).append((lookup_changed, url))

                if self.delay > 0:
                    time.sleep(self.delay)

            except Exception:
                continue

        for (dir_path, name), pages in files_by_name.items():
            # Give the original filename to a URL the old lookup could read.
            for index, (_, url) in enumerate(sorted(pages)):
                filename = name
                if index:
                    suffix = hashlib.sha256(url.encode()).hexdigest()[:12]
                    stem = f"{name.removesuffix('.txt')}-{suffix}"
                    filename = f"{stem}.txt"
                    while (dir_path, filename) in files_by_name or (dir_path, filename) in self._files:
                        stem += "_"
                        filename = f"{stem}.txt"

                self._files[(dir_path, filename)] = url
                content = self._cache[url].encode()
                entries.append(
                    ManifestEntry(
                        filename=filename,
                        path=dir_path,
                        checksum=hashlib.sha256(content).hexdigest()[:16],
                        size=len(content),
                    )
                )

        entries.sort(key=lambda e: e.display_path)
        return entries

    def _discover_urls(self) -> list[str]:
        """Discover URLs from sitemap.xml or by crawling links."""
        # Try sitemap first.
        if self.url.endswith(".xml"):
            return self._parse_sitemap(self.url)

        sitemap_url = f"{self._domain}/sitemap.xml"
        try:
            urls = self._parse_sitemap(sitemap_url)
            if urls:
                return urls
        except Exception:
            pass

        # Fall back to crawling.
        return self._crawl_links()

    def _parse_sitemap(self, url: str) -> list[str]:
        """Parse a sitemap.xml and return all URLs."""
        resp = self._http.get(url)
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        urls: list[str] = []

        # Handle sitemap index.
        for sitemap in root.findall(".//sm:sitemap/sm:loc", ns):
            if sitemap.text:
                urls.extend(self._parse_sitemap(sitemap.text))

        # Handle regular sitemap.
        for loc in root.findall(".//sm:url/sm:loc", ns):
            if loc.text:
                urls.append(loc.text)

        return urls

    def _crawl_links(self) -> list[str]:
        """Crawl same-domain links starting from the root URL."""
        visited: set[str] = set()
        queue = [self.url]
        urls: list[str] = []

        while queue and len(urls) < self.max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            urls.append(url)

            try:
                resp = self._http.get(url)
                resp.raise_for_status()

                # Extract same-domain links.
                for match in re.finditer(r'href=["\']([^"\']+)["\']', resp.text):
                    link = urljoin(url, match.group(1))
                    parsed = urlparse(link)
                    # Same domain, no fragments, no query params.
                    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if parsed.netloc == self._parsed.netloc and clean not in visited:
                        queue.append(clean)

                if self.delay > 0:
                    time.sleep(self.delay)
            except Exception:
                continue

        return urls

    def _fetch_page(self, url: str) -> str:
        """Fetch a page and extract text."""
        if url in self._cache:
            return self._cache[url]

        resp = self._http.get(url)
        resp.raise_for_status()
        return _html_to_text(resp.text)

    def read_file(self, path: str, filename: str) -> bytes:
        """Return cached page content."""
        url = self._files.get((path, filename))
        if url is None:
            target = f"{path}/{filename}" if path else filename
            raise FileNotFoundError(f"Page not in cache: {target}")
        return self._cache[url].encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_web_source(source: str) -> dict[str, str | None]:
    """Parse a web:URL source string."""
    url = source.removeprefix("web:")
    if not url.startswith("http"):
        url = f"https://{url}"
    return {"url": url}
