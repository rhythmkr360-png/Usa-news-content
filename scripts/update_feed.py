#!/usr/bin/env python3
"""Create the public, version-1 JSON feed consumed by USA-NEWS Flutter.

Works with Python 3.11+ and the standard library. Optional OpenAI summaries
require an API key in the environment. Never put tokens in public source files.
"""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("military-news")
UTC = timezone.utc
USER_AGENT = "MilitaryBriefFeed/1.0 (+https://github.com/rhythmkr360-png/Usa-news-content)"
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
SPORTS_TERMS = re.compile(
    r"\b(football|basketball|baseball|soccer|touchdown|gameday|"
    r"quarterback|halftime|playoffs|homecoming game|varsity)\b",
    re.IGNORECASE,
)
MILITARY_TERMS = re.compile(
    r"\b(military|defen[sc]e|pentagon|army|navy|naval|marine corps|marines|"
    r"air force|aircraft carrier|fighter jet|destroyer|submarine|troops|"
    r"service members?|soldiers?|airmen|sailors?|space force|weapon(?:s|ry)?|"
    r"missile|combat|deployment|drone|veterans?|national guard|nato|"
    r"dod|usaf|usmc|uss|aircraft|bomber)\b",
    re.IGNORECASE,
)
US_TERMS = re.compile(
    r"(?:\bu\.s\.(?:a\.)?(?=\W|$)|"
    r"\b(?:united states|american|pentagon|department of defen[sc]e|"
    r"usaf|usmc|ussf|us navy|us army|us air force|"
    r"uss [a-z]|national guard|us coast guard)\b)",
    re.IGNORECASE,
)
CATEGORY_TERMS = [
    ("Navy", re.compile(r"\b(navy|naval|sailor|carrier|destroyer|submarine|uss|shipbuilding|fleet)\b", re.I)),
    ("Air Force", re.compile(r"\b(air force|usaf|airmen|bomber|fighter jet|f-35|f-22|b-52|aircraft)\b", re.I)),
    ("Army", re.compile(r"\b(army|soldier|infantry|armored|abrams|artillery|national guard)\b", re.I)),
    ("Marine Corps", re.compile(r"\b(marines|marine corps|usmc|marine expeditionary)\b", re.I)),
    ("Space Force", re.compile(r"\b(space force|ussf|space command|satellite)\b", re.I)),
    ("Defense", re.compile(r"\b(drone|missile|weapon|radar|technology|hypersonic|cyber|ai system)\b", re.I)),
]


class TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "li"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def clean_text(value: str | None, limit: int = 1000) -> str:
    parser = TextOnly()
    parser.feed(value or "")
    value = html.unescape(" ".join(parser.parts))
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit].strip()


def https_url(raw: str | None) -> str | None:
    """Reject non-HTTPS, credentials, and URLs without ordinary hostnames."""
    if not raw:
        return None
    try:
        p = urlsplit(raw.strip())
        if p.scheme.lower() != "https" or not p.hostname or "@" in p.netloc:
            return None
        if p.port not in (None, 443):
            return None
        if not re.fullmatch(r"[a-z0-9.-]+", p.hostname.lower()):
            return None
        # Remove standard tracking parameters while keeping query-based news links.
        query = urlencode(
            [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not (k.lower().startswith("utm_") or k.lower() in {"fbclid", "gclid"})]
        )
        return urlunsplit(("https", p.netloc.lower(), p.path or "/", query, ""))
    except ValueError:
        return None


def publish_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            value = parsedate_to_datetime(raw)
        except (ValueError, TypeError, IndexError):
            return None
    return value.replace(tzinfo=UTC).astimezone(UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def element_text(parent: ET.Element, *paths: str) -> str:
    for path in paths:
        element = parent.find(path)
        if element is not None:
            value = "".join(element.itertext()).strip()
            if value:
                return value
    return ""


def parse_syndication(xml: bytes, source_name: str) -> list[dict[str, str]]:
    """Read RSS 2.0 and Atom 1.0. One bad item never stops other items."""
    root = ET.fromstring(xml)
    records: list[dict[str, str]] = []
    if root.tag in {"rss", "RDF", "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"}:
        items = root.findall(".//item")
    elif root.tag == f"{ATOM}feed":
        items = root.findall(f"{ATOM}entry")
    else:
        raise ValueError("Unsupported RSS or Atom document")

    for item in items:
        try:
            atom = item.tag == f"{ATOM}entry"
            title = clean_text(element_text(item, f"{ATOM}title" if atom else "title"), 230)
            desc = clean_text(element_text(item, f"{ATOM}summary", f"{ATOM}content") if atom else
                              element_text(item, "description", f"{CONTENT}encoded"))
            link = element_text(item, "link") if not atom else ""
            if atom:
                for element in item.findall(f"{ATOM}link"):
                    if element.attrib.get("rel", "alternate") == "alternate":
                        link = element.attrib.get("href", "")
                        break
            source = element_text(item, "source", f"{ATOM}source/{ATOM}title") or source_name
            date = element_text(item, f"{ATOM}published", f"{ATOM}updated") if atom else element_text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date")
            # DVIDS journalism generally allows reuse with credit. Restrict
            # extracted media to DVIDS-hosted image links, never publisher photos.
            photo = ""
            if source_name.lower().startswith("dvids"):
                images = list(item.findall(f"{MEDIA}thumbnail"))
                images += list(item.findall(f"{MEDIA}content"))
                images += list(item.findall("enclosure"))
                for media in images:
                    media_url = https_url(media.attrib.get("url", ""))
                    media_type = media.attrib.get("type", "").lower()
                    if not media_url or media_type and not media_type.startswith("image/"):
                        continue
                    host = urlsplit(media_url).hostname or ""
                    if host == "dvidshub.net" or host.endswith(".dvidshub.net"):
                        photo = media_url
                        break
            if title and link:
                records.append({"title": title, "description": desc, "link": link,
                                "sourceName": clean_text(source, 90) or source_name,
                                "publishedAt": date, "imageUrl": photo})
        except (ValueError, TypeError):
            LOG.warning("Skipping one malformed RSS item from %s", source_name)
    return records


def rss_url(source: dict[str, str]) -> str:
    if source.get("type") == "google_news":
        return ("https://news.google.com/rss/search?q=" + quote(source["query"] + " when:2d")
                + "&hl=en-US&gl=US&ceid=US%3Aen")
    return source["url"]


def download(url: str, retries: int = 2) -> bytes:
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
            with urlopen(request, timeout=18) as response:
                if response.status != 200:
                    raise ValueError(f"HTTP status {response.status}")
                data = response.read(3_000_001)
                if len(data) > 3_000_000:
                    raise ValueError("Feed exceeds 3 MB")
                return data
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            if attempt >= retries or isinstance(error, HTTPError) and error.code < 500 and error.code != 429:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Unreachable")


def short_excerpt(value: str, limit: int = 350) -> str:
    if len(value) <= limit:
        return value
    clipped = value[:limit]
    sentence_ends = [match.end() for match in re.finditer(r"[.!?](?=\s|$)", clipped)]
    if sentence_ends and sentence_ends[-1] >= 110:
        return clipped[:sentence_ends[-1]]
    return clipped.rsplit(" ", 1)[0].rstrip(",;: ") + "…"


def informative_excerpt(title: str, description: str) -> bool:
    """Reject RSS fields that merely repeat the headline or publisher."""
    if len(description) < 65:
        return False
    title_words = set(re.findall(r"[a-z0-9]+", title.casefold()))
    description_words = set(re.findall(r"[a-z0-9]+", description.casefold()))
    novel = description_words - title_words
    return len(novel) >= max(5, len(description_words) // 5)


def candidate(raw: dict[str, str], now: datetime, maximum_age: timedelta) -> dict | None:
    title = clean_text(raw.get("title"), 220)
    source = clean_text(raw.get("sourceName"), 90)
    url = https_url(raw.get("link"))
    published = publish_time(raw.get("publishedAt"))
    description = clean_text(raw.get("description"), 850)
    if not title or len(title) < 18 or not source or not url or not published:
        return None
    if published > now + timedelta(minutes=15) or published < now - maximum_age:
        return None
    # Google News RSS descriptions often contain only a repeat of the headline,
    # publisher name, and HTML links. Do not invent a summary from these.
    if not informative_excerpt(title, description):
        return None
    evidence = title + " " + description
    if SPORTS_TERMS.search(title):
        return None
    if not MILITARY_TERMS.search(evidence) or not US_TERMS.search(evidence):
        return None
    category = next((name for name, pattern in CATEGORY_TERMS if pattern.search(title)), "Defense")
    article_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    result = {"id": article_id, "title": title, "summary": short_excerpt(description),
              "category": category, "sourceName": source, "sourceUrl": url,
              "publishedAt": iso(published)}
    if raw.get("imageUrl"):
        result["imageUrl"] = raw["imageUrl"]
    return result


def json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid existing state at {path}: {error}") from error


def store_json(path: Path, value: object) -> bool:
    """Don't touch unchanged files; avoid commits when no articles changed."""
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return True


def rewrite_with_openai(articles: list[dict], api_key: str, model: str, max_ai: int) -> None:
    """Optional concise rewrites grounded *only* in the supplied RSS excerpt."""
    if not api_key or max_ai <= 0:
        return
    for offset in range(0, min(len(articles), max_ai), 10):
        batch = articles[offset: min(offset + 10, max_ai)]
        content = [{"id": a["id"], "title": a["title"], "excerpt": a["summary"]} for a in batch]
        messages = [
            {"role": "system", "content": (
                "Edit the following military news excerpts for a factual U.S. news reader. "
                "Return ONLY a JSON object {\"items\":[{\"id\":\"...\",\"summary\":\"...\"}]}. "
                "Use ONLY stated facts, never invent names, figures, quotations, dates, results, or context. "
                "Do not change headlines or article links. Never write speculation as fact. "
                "One clear paragraph per item, 40-80 words maximum. Neutral, no sensationalism. "
                "If the excerpt is insufficient, return its factual content without additions.")},
            {"role": "user", "content": json.dumps(content, ensure_ascii=False)}]
        payload = json.dumps({"model": model, "messages": messages,
                              "response_format": {"type": "json_object"}, "temperature": 0.2}).encode()
        request = Request("https://api.openai.com/v1/chat/completions", data=payload,
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          method="POST")
        try:
            with urlopen(request, timeout=50) as response:
                response_data = json.load(response)
            rewritten = json.loads(response_data["choices"][0]["message"]["content"])
            by_id = {item.get("id"): item.get("summary") for item in rewritten.get("items", [])
                     if isinstance(item, dict)}
            for article in batch:
                summary = by_id.get(article["id"])
                if isinstance(summary, str):
                    summary = clean_text(summary, 480)
                    if 45 <= len(summary) <= 480:
                        article["summary"] = summary
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError) as error:
            LOG.warning("AI rewrite batch unavailable; keeping source excerpts: %s", error)


def publish(articles: list[dict], now: datetime, page_size: int) -> bool:
    """Keep the Flutter feed and its pagination consistent."""
    data_dir = ROOT / "data"
    existing = json_file(data_dir / "articles.json", [])
    seen_list = json_file(data_dir / "seen.json", [])
    if not isinstance(existing, list) or not isinstance(seen_list, list):
        raise RuntimeError("History files must contain JSON lists")
    seen = set(str(v) for v in seen_list)
    new_articles = [article for article in articles if article["id"] not in seen]
    # Even on a quiet day, retire stories outside the retention window.
    expiry_cutoff = now - timedelta(days=int(os.getenv("HISTORY_DAYS", "10")))
    expired = any(isinstance(a, dict) and
                  (not publish_time(a.get("publishedAt")) or
                   publish_time(a.get("publishedAt")) < expiry_cutoff)
                  for a in existing)
    uninformative = any(isinstance(a, dict) and not informative_excerpt(
        str(a.get("title", "")), str(a.get("summary", "")))
        for a in existing)
    if not new_articles and not expired and not uninformative:
        LOG.info("No new eligible stories; leaving existing feed unchanged")
        return False

    max_age_days = int(os.getenv("HISTORY_DAYS", "10"))
    max_items = int(os.getenv("MAX_STORED_ARTICLES", "1000"))
    cutoff = now - timedelta(days=max_age_days)
    merged = {
        a["id"]: a for a in existing
        if isinstance(a, dict) and "id" in a
        and publish_time(a.get("publishedAt"))
        and informative_excerpt(str(a.get("title", "")), str(a.get("summary", "")))
    }
    for article in new_articles:
        merged[article["id"]] = article
    ordered = sorted(
        (a for a in merged.values() if publish_time(a.get("publishedAt")) >= cutoff),
        key=lambda a: (a["publishedAt"], a["id"]), reverse=True
    )[:max_items]

    # More than a single run's output stays in history; older IDs won't come back.
    seen_list = list(dict.fromkeys([a["id"] for a in ordered] + seen_list + [a["id"] for a in new_articles]))[:20_000]
    changed = store_json(data_dir / "articles.json", ordered)
    changed = store_json(data_dir / "seen.json", seen_list) or changed
    pages = [ordered[i:i + page_size] for i in range(0, len(ordered), page_size)] or [[]]
    timestamp = iso(now)
    for i, subset in enumerate(pages):
        index = i + 1
        file_path = ROOT / "feed.json" if index == 1 else ROOT / "pages" / f"page-{index}.json"
        next_path = (f"pages/page-{index + 1}.json" if index == 1 else f"page-{index + 1}.json") if i + 1 < len(pages) else None
        value = {"version": 1, "generatedAt": timestamp, "articles": subset}
        if next_path:
            value["nextPage"] = next_path
        changed = store_json(file_path, value) or changed
    for stale in (ROOT / "pages").glob("page-*.json") if (ROOT / "pages").exists() else []:
        if stale not in {ROOT / "pages" / f"page-{i}.json" for i in range(2, len(pages) + 1)}:
            stale.unlink()
            changed = True
    return changed


def run(now: datetime, limit: int, dry_run: bool) -> int:
    config = json_file(ROOT / "sources.json", [])
    if not isinstance(config, list) or not config:
        raise RuntimeError("sources.json must be a non-empty list")
    collected: dict[str, dict] = {}
    succeeded = 0
    for source in config:
        name = source["name"]
        url = rss_url(source)
        try:
            if not https_url(url):
                raise ValueError("Feed URL must be public HTTPS")
            records = parse_syndication(download(url), name)
            succeeded += 1
            usable = 0
            for record in records[:int(source.get("max_items", 200))]:
                article = candidate(record, now, timedelta(hours=int(os.getenv("MAX_AGE_HOURS", "72"))))
                if article is not None:
                    collected.setdefault(article["id"], article)
                    usable += 1
            LOG.info("%s: %d entries, %d eligible", name, len(records), usable)
        except (HTTPError, URLError, TimeoutError, ET.ParseError, ValueError, KeyError) as error:
            LOG.warning("Skipping unavailable source %s: %s", name, error)
    if succeeded == 0:
        LOG.error("Every source failed; previous published news remains untouched")
        return 1
    new = sorted(collected.values(), key=lambda item: item["publishedAt"], reverse=True)
    history = json_file(ROOT / "data" / "seen.json", [])
    seen = set(history)
    new = [item for item in new if item["id"] not in seen][:limit]
    LOG.info("%d new articles qualified after validation and deduplication", len(new))
    if dry_run:
        LOG.info("Dry run: writing nothing")
        return 0
    rewrite_with_openai(new, os.getenv("OPENAI_API_KEY", ""),
                        os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                        int(os.getenv("MAX_AI_ARTICLES", "100")))
    publish(new, now, page_size=int(os.getenv("PAGE_SIZE", "40")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the U.S. military news feed")
    parser.add_argument("--dry-run", action="store_true", help="Check feeds without writing files")
    parser.add_argument("--limit", type=int, default=int(os.getenv("MAX_NEW_PER_RUN", "150")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    return run(datetime.now(UTC), args.limit, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

