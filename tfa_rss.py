#!/usr/bin/env python3
"""Unofficial RSS 2.0 feeds for todayforai.com -- stdlib only, no API key.

Why this exists: todayforai.com has no working RSS endpoint. Its own
llms.txt advertises /api/v1/feed as "RSS/Atom", but that endpoint returns
JSON and holds only glossary (explore) entities. The public anonymous agent
API documented at https://todayforai.com/en/agent is the real content source.

Outputs (written next to each other by `gen`):
  feed.xml     latest intelligence, 24h window, up to 30 items
  hot.xml      trending story radar, top 10, links to site story pages
  index.html   tiny landing page linking both feeds

Usage:
  python tfa_rss.py gen --out . --base-url https://raw.githubusercontent.com/<u>/<r>/main
  python tfa_rss.py serve --port 8787

TinyFish (optional, only when a request fails) is used exclusively through its
free Fetch API: 1,000 URLs/day, max 4 calls per run. Nothing metered is called.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = "https://todayforai.com"
# Cost policy: TinyFish is used for ONE product only -- the Fetch API, which is
# free up to 1,000 URLs/day (150 fetches/min). Never agent / research / browser /
# monitor endpoints: those are metered. Both the import-time check and the
# runtime check below fail closed if this constant is ever pointed elsewhere.
_TINYFISH_ALLOWED = {"https://api.fetch.tinyfish.ai"}
TINYFISH_FETCH = "https://api.fetch.tinyfish.ai"
TINYFISH_MAX_CALLS = 4  # hard cap per process; a normal run needs 0, worst case 2
_TINYFISH_CALLS = 0
_TINYFISH_OFF = False

if TINYFISH_FETCH not in _TINYFISH_ALLOWED:
    raise RuntimeError("only the free TinyFish Fetch endpoint may be used")

UA = "tfa-rss/2.0 (github.com feed builder)"
TIMEOUT = 25

esc = html.escape

# ---------------------------------------------------------------- http helpers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the X-API-Key header can never be forwarded elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(
            req.full_url, code, "refusing redirect while sending an API key", headers, fp
        )


def _key() -> str:
    return os.environ.get("TINYFISH_API_KEY", "").strip()


def _can_tinyfish() -> bool:
    """Fallback is opt-in by secret presence and can be force-disabled with --no-tinyfish."""
    return not _TINYFISH_OFF and bool(_key())


def disable_tinyfish(off: bool = True) -> None:
    global _TINYFISH_OFF
    _TINYFISH_OFF = off


def redact(text: str) -> str:
    """Last-line defence: strip the key from anything we print or write."""
    key = _key()
    return text.replace(key, "[redacted]") if key and key in text else text


def _fetch_endpoint() -> str:
    """Fail closed unless the endpoint is the free Fetch API."""
    if TINYFISH_FETCH not in _TINYFISH_ALLOWED:
        raise RuntimeError("only the free TinyFish Fetch endpoint may be used")
    return TINYFISH_FETCH


def _tinyfish(url: str) -> str:
    """Free-tier Fetch API only (1,000 URLs/day). Nothing metered is ever called."""
    global _TINYFISH_CALLS
    key = _key()
    if not key:
        raise RuntimeError("TINYFISH_API_KEY not set")
    if _TINYFISH_CALLS >= TINYFISH_MAX_CALLS:
        raise RuntimeError(f"tinyfish budget for this run exhausted ({TINYFISH_MAX_CALLS})")
    body = json.dumps({"urls": [url], "format": "markdown"}).encode()
    req = urllib.request.Request(
        _fetch_endpoint(),
        data=body,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    _TINYFISH_CALLS += 1
    try:
        with opener.open(req, timeout=60) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # status + URL only; never echo request headers or the body
        raise RuntimeError(f"tinyfish http {e.code} for {url}") from None
    results = payload.get("results") or []
    if not results or not results[0].get("text"):
        raise RuntimeError(f"tinyfish fetch failed: {redact(str(payload.get('errors')))}")
    return results[0]["text"]


def _get(url: str) -> str:
    # keep urllib pinned to https + this host (no file:// or foreign schemes)
    if not url.startswith(f"{SITE}/"):
        raise ValueError(f"refusing non-site url: {url}")

    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"}),
                timeout=TIMEOUT,
            ) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < 2:
                time.sleep(min(float(e.headers.get("Retry-After") or 2), 10))
                continue
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < 2:
                time.sleep(2)

    if _can_tinyfish():
        print(f"[warn] {url} failed ({redact(str(last))}); retrying via TinyFish Fetch", file=sys.stderr)
        return _tinyfish(url)
    raise last or RuntimeError(f"fetch failed: {url}")


def get_json(path: str, **params) -> dict:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return json.loads(_get(f"{SITE}{path}" + (f"?{q}" if q else "")))


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# --------------------------------------------------- english text (best effort)
# /api/v1/agent/latest is Chinese-only. The English news page ships the same
# article ids with title_en / summary_en inside its Next.js payload, so we read
# it once and merge by id. Any failure degrades to the Chinese API text.

_ART_ID = re.compile(r'"id":"(art-[^"]+)"')
_EN_FIELDS = {
    k: re.compile(rf'"{k}":"((?:[^"\\]|\\.)*)"')
    for k in ("title_en", "summary_en", "published_at", "source_name")
}


def english_meta() -> dict[str, dict]:
    try:
        raw = _get(f"{SITE}/en/news")
    except Exception as e:  # noqa: BLE001 - enrichment is optional
        print(f"[warn] english enrichment skipped: {e}", file=sys.stderr)
        return {}
    txt = raw.replace('\\"', '"').replace("\\n", " ").replace("\\u0026", "&")
    out: dict[str, dict] = {}
    for m in _ART_ID.finditer(txt):
        seg = txt[m.start() : m.start() + 4000]
        row = {}
        for key, rx in _EN_FIELDS.items():
            found = rx.search(seg)
            if found:
                try:
                    row[key] = json.loads(f'"{found.group(1)}"')
                except ValueError:
                    pass
        if row.get("title_en"):
            out[m.group(1)] = row
    return out


# ----------------------------------------------------------------- feed items
# item = dict(title, link, guid, published: datetime|None, html, tags, source)


def latest_items(
    *, window="24h", mode="all", category=None, limit=30, english=True
) -> list[dict]:
    data = get_json(
        "/api/v1/agent/latest",
        window=window,
        mode=mode,
        category=category,
        limit=limit,
    )
    en = english_meta() if english else {}
    items, seen = [], set()
    for it in data.get("items") or []:
        link = it.get("url") or ""
        if not link or link in seen:
            continue
        seen.add(link)
        e = en.get(it.get("id", ""), {})
        title = e.get("title_en") or it.get("title") or "(untitled)"
        body = []
        if e.get("summary_en") or it.get("summary"):
            body.append(f"<p>{esc(e.get('summary_en') or it['summary'])}</p>")
        if e.get("title_en") and it.get("title"):
            body.append(f"<p><i>原文标题：</i>{esc(it['title'])}</p>")
        if it.get("reason"):
            body.append(f"<p><i>Why it matters：</i>{esc(it['reason'])}</p>")
        meta = [x for x in (it.get("source"), it.get("category")) if x]
        if it.get("score"):
            meta.append(f"signal {it['score']}/100")
        if meta:
            body.append("<p>" + esc(" · ".join(meta)) + "</p>")
        items.append(
            {
                "title": title,
                "link": link,
                "guid": link,
                "published": _dt(e.get("published_at") or it.get("published_at")),
                "html": "".join(body),
                "tags": (it.get("tags") or [])[:6],
                "source": it.get("source") or e.get("source_name"),
            }
        )
    return items[:limit]


def hot_items(*, limit=10) -> list[dict]:
    data = get_json("/api/v1/agent/hot", limit=limit)
    items = []
    for it in data.get("items") or []:
        sid = it.get("story_id") or ""
        link = f"{SITE}/en/story/{sid}" if sid else SITE
        body = []
        summary = (it.get("summary") or "").strip()
        if summary:
            body.append(f"<p>{esc(summary[:700])}{'…' if len(summary) > 700 else ''}</p>")
        bits = []
        if it.get("heat") is not None:
            bits.append(f"heat {it['heat']}")
        if it.get("source_count"):
            bits.append(f"{it['source_count']} sources")
        if bits:
            body.append("<p>" + esc(" · ".join(bits)) + "</p>")
        if it.get("source_names"):
            body.append("<p>" + esc(", ".join(it["source_names"][:6])) + "</p>")
        items.append(
            {
                "title": it.get("title") or "(untitled)",
                "link": link,
                "guid": link,
                "published": _dt(it.get("latest_at")),
                "html": "".join(body),
                "tags": [],
                "source": None,
            }
        )
    return items


# ------------------------------------------------------------------- rss build


def _stamp(items: list[dict]) -> datetime:
    """Newest item date, so an unchanged feed stays byte-identical (no commit churn)."""
    dates = [it["published"] for it in items if it.get("published")]
    return max(dates) if dates else datetime.now(timezone.utc)


def build_feed(
    *,
    title: str,
    link: str,
    description: str,
    items: list[dict],
    self_url: str | None = None,
) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{esc(title)}</title>",
        f"<link>{esc(link)}</link>",
        f"<description>{esc(description)}</description>",
        "<language>en</language>",
        f"<lastBuildDate>{format_datetime(_stamp(items))}</lastBuildDate>",
        "<ttl>10</ttl>",
        "<generator>tfa_rss.py</generator>",
    ]
    if self_url:
        out.append(
            f'<atom:link href="{esc(self_url)}" rel="self" type="application/rss+xml"/>'
        )
    for it in items:
        out.append("<item>")
        out.append(f"<title>{esc(it['title'])}</title>")
        out.append(f"<link>{esc(it['link'])}</link>")
        out.append(f'<guid isPermaLink="true">{esc(it["guid"])}</guid>')
        if it["published"]:
            out.append(f"<pubDate>{format_datetime(it['published'])}</pubDate>")
        if it["html"]:
            out.append(f"<description>{esc(it['html'])}</description>")
        if it.get("source"):
            out.append(f'<source url="{esc(it["link"])}">{esc(it["source"])}</source>')
        for tag in it.get("tags") or []:
            out.append(f"<category>{esc(tag)}</category>")
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


def news_feed(self_url=None, **kw) -> str:
    return build_feed(
        title="Today for AI — Latest Intelligence",
        link=f"{SITE}/en/news",
        description="Curated frontier AI news, 24h window, from todayforai.com.",
        items=latest_items(**kw),
        self_url=self_url,
    )


def hot_feed(self_url=None, **kw) -> str:
    return build_feed(
        title="Today for AI — Hot Radar",
        link=f"{SITE}/en/hot",
        description="Multi-source trending AI stories ranked by cross-source heat.",
        items=hot_items(**kw),
        self_url=self_url,
    )


INDEX = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Today for AI — RSS</title>
<style>
 body{{font:16px/1.6 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1.2rem;color:#111}}
 a{{color:#0b62d0}} li{{margin:.4rem 0}} .muted{{color:#666;font-size:.9rem}}
</style>
<h1>Today for AI — RSS</h1>
<p>Unofficial RSS 2.0 feeds for <a href="https://todayforai.com">todayforai.com</a>,
generated from the site's public anonymous agent API.</p>
<ul>
 <li><a href="feed.xml">feed.xml</a> — latest intelligence, 24h window ({n} items)</li>
 <li><a href="hot.xml">hot.xml</a> — trending story radar ({m} stories)</li>
</ul>
<p class="muted">{note}</p>
"""


def index_html(n: str, m: str, note: str) -> str:
    return INDEX.format(n=n, m=m, note=note)


def write_out(out_dir: Path, news: str, hot: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    key = _key()
    if key and (key in news or key in hot):
        # loud failure beats silently rewriting an echoed credential
        raise SystemExit("error: upstream echoed the API key; refusing to write output")
    files = {
        "feed.xml": redact(news),
        "hot.xml": redact(hot),
        "index.html": redact(
            index_html(
                news.count("<item>"),
                hot.count("<item>"),
                "Rebuilt by GitHub Actions; item links point at the original sources.",
            )
        ),
    }
    for name, body in files.items():
        if key and key in body:
            raise SystemExit(f"error: {name} would contain the API key")
    for name, body in files.items():
        (out_dir / name).write_text(body, encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ http serve


class Cache:
    """Serve the last good XML even if a refresh fails."""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.data: dict[str, tuple[str, float]] = {}

    def get(self, key: str, builder) -> str:
        with self.lock:
            xml, ts = self.data.get(key, ("", 0.0))
            if xml and time.time() - ts < self.ttl:
                return xml
            try:
                xml = builder()
                self.data[key] = (xml, time.time())
            except Exception as e:  # noqa: BLE001 - stale beats broken
                if not xml:
                    raise
                print(f"[warn] refresh failed ({key}): {e}", file=sys.stderr)
            return xml


def make_handler(cache: Cache, opts: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tfa-rss/2.0"

        def _send(self, body: str, ctype: str, code: int = 200) -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            host = self.headers.get("Host") or f"{opts['host']}:{opts['port']}"
            base = f"http://{host}"
            try:
                if path in ("/", "/index.html"):
                    n = cache.get("news", lambda: news_feed(f"{base}/feed.xml", **opts["news"]))
                    h = cache.get("hot", lambda: hot_feed(f"{base}/hot.xml", **opts["hot"]))
                    self._send(
                        index_html(n.count("<item>"), h.count("<item>"), "Served locally."),
                        "text/html; charset=utf-8",
                    )
                elif path == "/feed.xml":
                    self._send(
                        cache.get("news", lambda: news_feed(f"{base}/feed.xml", **opts["news"])),
                        "application/rss+xml; charset=utf-8",
                    )
                elif path == "/hot.xml":
                    self._send(
                        cache.get("hot", lambda: hot_feed(f"{base}/hot.xml", **opts["hot"])),
                        "application/rss+xml; charset=utf-8",
                    )
                elif path == "/healthz":
                    self._send("ok", "text/plain; charset=utf-8")
                else:
                    self._send("not found", "text/plain; charset=utf-8", 404)
            except Exception as e:  # noqa: BLE001
                self._send(f"upstream error: {e}", "text/plain; charset=utf-8", 502)

        def log_message(self, fmt: str, *args) -> None:
            print(f"[{self.log_date_time_string()}] {fmt % args}", file=sys.stderr)

    return Handler


# ----------------------------------------------------------------------- cli


def _check(xml: str, label: str) -> int:
    ET.fromstring(xml)  # raises on malformed output
    return xml.count("<item>")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Unofficial RSS feeds for todayforai.com")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="write feed.xml, hot.xml and index.html")
    g.add_argument("--out", default=".")
    g.add_argument(
        "--base-url",
        default=os.environ.get("FEED_BASE_URL", ""),
        help="public directory URL, used for atom:link rel=self",
    )
    g.add_argument("--window", default="24h", choices=["24h", "7d"])
    g.add_argument("--mode", default="all", choices=["all", "selected"])
    g.add_argument(
        "--category", default=None, choices=["llm", "agent", "opensource", "tools", "compute"]
    )
    g.add_argument("--limit", type=int, default=30)
    g.add_argument("--hot-limit", type=int, default=10)
    g.add_argument("--no-en", action="store_true", help="skip English enrichment")
    g.add_argument(
        "--no-tinyfish", action="store_true", help="never call TinyFish, even if a key is set"
    )

    s = sub.add_parser("serve", help="serve feeds over HTTP")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--ttl", type=int, default=600, help="refresh interval in seconds")
    s.add_argument("--window", default="24h", choices=["24h", "7d"])
    s.add_argument("--mode", default="all", choices=["all", "selected"])
    s.add_argument("--category", default=None)
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--no-en", action="store_true")
    s.add_argument("--no-tinyfish", action="store_true")

    a = p.parse_args(argv)

    if getattr(a, "no_tinyfish", False):
        disable_tinyfish()

    if a.cmd == "gen":
        base = a.base_url.rstrip("/") or None
        news = news_feed(
            f"{base}/feed.xml" if base else None,
            window=a.window,
            mode=a.mode,
            category=a.category,
            limit=a.limit,
            english=not a.no_en,
        )
        hot = hot_feed(f"{base}/hot.xml" if base else None, limit=a.hot_limit)
        n_items = _check(news, "feed.xml")
        if n_items == 0:
            raise SystemExit("error: upstream returned no items, refusing to write an empty feed")
        write_out(Path(a.out), news, hot)
        print(f"wrote {a.out}/feed.xml ({n_items} items)")
        print(f"wrote {a.out}/hot.xml ({hot.count('<item>')} items)")
        print(f"wrote {a.out}/index.html")
        return 0

    opts = {
        "host": a.host,
        "port": a.port,
        "news": {
            "window": a.window,
            "mode": a.mode,
            "category": a.category,
            "limit": a.limit,
            "english": not a.no_en,
        },
        "hot": {"limit": 10},
    }
    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(Cache(a.ttl), opts))
    print(f"serving http://{a.host}:{a.port}/feed.xml and /hot.xml (ttl {a.ttl}s)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
