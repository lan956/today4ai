#!/usr/bin/env python3
"""Unofficial RSS 2.0 feeds for todayforai.com -- stdlib only, 2-3 HTTP calls."""
from __future__ import annotations

import html, json, os, re, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

SITE = "https://todayforai.com"
KEY = os.getenv("TINYFISH_API_KEY", "").strip()
calls = 0
esc = html.escape
redact = lambda s: s.replace(KEY, "[redacted]") if KEY and KEY in s else s


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        raise urllib.error.HTTPError(a[0].full_url, a[2], "redirect blocked", a[3], a[1])


def tinyfish(url: str) -> str:
    # ponytail: Hardcode the free Fetch endpoint directly in Request instead of maintaining an allowlist set; extract to config if multi-provider fallback is needed.
    global calls
    if calls >= 4:
        raise RuntimeError("tinyfish call cap (4) reached")
    calls += 1
    req = urllib.request.Request(
        "https://api.fetch.tinyfish.ai",
        json.dumps({"urls": [url], "format": "markdown"}).encode(),
        {"X-API-Key": KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as r:  # nosemgrep
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"tinyfish http {e.code} for {url}") from None
    if not (res := out.get("results")) or not res[0].get("text"):
        raise RuntimeError(f"tinyfish failed: {redact(str(out.get('errors')))}")
    return res[0]["text"]


def fetch(url: str) -> str:
    if not url.startswith(f"{SITE}/"):
        raise ValueError(f"refusing non-site url: {url}")
    err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tfa-rss/3.0", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=25) as r:  # nosemgrep
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            err = e
            if e.code != 429 or attempt == 2:
                break
            time.sleep(min(float(e.headers.get("Retry-After") or 2), 10))
        except (urllib.error.URLError, TimeoutError) as e:
            err = e
            time.sleep(2)
    if KEY:
        print(f"[warn] {url} failed ({redact(str(err))}); via TinyFish", file=sys.stderr)
        return tinyfish(url)
    raise err  # type: ignore[misc]


def dt(v: str | None) -> datetime | None:
    # ponytail: Rely on Python 3.11+ stdlib datetime.fromisoformat (supports 'Z' natively); add custom parsing only if upstream emits non-ISO dates.
    try:
        d = datetime.fromisoformat(v) if v else None
        return d and (d if d.tzinfo else d.replace(tzinfo=timezone.utc))
    except ValueError:
        return None


def english() -> dict[str, dict]:
    # ponytail: Scrape Next.js payload via regex slices instead of a full HTML/AST parser; switch to an English JSON API endpoint if upstream adds one.
    try:
        txt = fetch(f"{SITE}/en/news").replace('\\"', '"').replace("\\n", " ").replace("\\u0026", "&")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] no english text: {redact(str(e))}", file=sys.stderr)
        return {}
    out = {}
    for m in re.finditer(r'"id":"(art-[^"]+)"', txt):
        seg = txt[m.start() : m.start() + 4000]
        row = {f: g[1] for f in ("title_en", "summary_en", "published_at") if (g := re.search(rf'"{f}":"((?:[^"\\]|\\.)*)"', seg))}
        if row.get("title_en"):
            out[m[1]] = row
    return out


def items() -> list[dict]:
    # ponytail: Inline fixed query params instead of a generic urllib.parse wrapper; reintroduce query builder if CLI filtering flags are needed.
    en, out, seen = english(), [], set()
    for it in json.loads(fetch(f"{SITE}/api/v1/agent/latest?window=24h&mode=all&limit=30")).get("items") or []:
        if not (link := it.get("url")) or link in seen:
            continue
        seen.add(link)
        e = en.get(it.get("id", ""), {})
        meta = [x for x in (it.get("source"), it.get("category"), f"signal {it['score']}/100" if it.get("score") else None) if x]
        ps = [
            esc(s) if (s := e.get("summary_en") or it.get("summary")) else None,
            f"<i>原文标题：</i>{esc(it['title'])}" if e.get("title_en") and it.get("title") else None,
            f"<i>Why it matters：</i>{esc(it['reason'])}" if it.get("reason") else None,
            esc(" · ".join(meta)) if meta else None,
        ]
        out.append({
            "title": e.get("title_en") or it.get("title") or "(untitled)",
            "link": link,
            "pub": dt(e.get("published_at") or it.get("published_at")),
            "html": "".join(f"<p>{p}</p>" for p in ps if p),
            "tags": (it.get("tags") or [])[:6],
            "source": it.get("source"),
        })
    return out[:30]


def hot() -> list[dict]:
    out = []
    for it in json.loads(fetch(f"{SITE}/api/v1/agent/hot?limit=10")).get("items") or []:
        s = (it.get("summary") or "").strip()
        ps = [
            f"{esc(s[:700])}{'…' if len(s) > 700 else ''}" if s else None,
            f"{it['source_count']} sources" if it.get("source_count") else None,
            esc(", ".join(it["source_names"][:6])) if it.get("source_names") else None,
        ]
        out.append({
            "title": it.get("title") or "(untitled)",
            "link": f"{SITE}/en/story/{sid}" if (sid := it.get("story_id")) else SITE,
            "pub": dt(it.get("latest_at")),
            "html": "".join(f"<p>{p}</p>" for p in ps if p),
        })
    return out


def rss(title: str, link: str, desc: str, rows: list[dict], self_url: str | None) -> str:
    stamp = max((r["pub"] for r in rows if r["pub"]), default=datetime.now(timezone.utc))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        f"<channel><title>{esc(title)}</title><link>{esc(link)}</link><description>{esc(desc)}</description>",
        f"<language>en</language><lastBuildDate>{format_datetime(stamp)}</lastBuildDate><ttl>10</ttl>",
    ]
    if self_url:
        out.append(f'<atom:link href="{esc(self_url)}" rel="self" type="application/rss+xml"/>')
    for r in rows:
        out.append(f'<item><title>{esc(r["title"])}</title><link>{esc(r["link"])}</link><guid isPermaLink="true">{esc(r["link"])}</guid>')
        if r["pub"]:
            out.append(f"<pubDate>{format_datetime(r['pub'])}</pubDate>")
        if r["html"]:
            out.append(f"<description>{esc(r['html'])}</description>")
        if r.get("source"):
            out.append(f'<source url="{esc(r["link"])}">{esc(r["source"])}</source>')
        out += [f"<category>{esc(t)}</category>" for t in r.get("tags", ())] + ["</item>"]
    return "\n".join(out + ["</channel>", "</rss>", ""])


def main() -> int:
    # ponytail: Read config from env/argv tail instead of argparse subparsers; upgrade to argparse if CLI flags grow.
    out = Path(sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else ".")
    base = (sys.argv[sys.argv.index("--base-url") + 1] if "--base-url" in sys.argv else os.getenv("FEED_BASE_URL", "")).rstrip("/")
    news = rss("Today for AI — Latest Intelligence", f"{SITE}/en/news", "Curated frontier AI news, 24h window, from todayforai.com.", items(), f"{base}/feed.xml" if base else None)
    radar = rss("Today for AI — Hot Radar", f"{SITE}/en/hot", "Multi-source trending AI stories ranked by cross-source heat.", hot(), f"{base}/hot.xml" if base else None)
    if "<item>" not in news:
        raise SystemExit("error: upstream returned no items; refusing to write an empty feed")
    ET.fromstring(news)
    ET.fromstring(radar)
    idx = f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Today for AI — RSS</title><style>body{{font:16px/1.6 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}a{{color:#0b62d0}}.muted{{color:#666;font-size:.9rem}}</style><h1>Today for AI — RSS</h1><p>Unofficial feeds for <a href="{SITE}">todayforai.com</a>, built from its public agent API.</p><ul><li><a href="feed.xml">feed.xml</a> — latest intelligence, 24h window ({news.count("<item>")} items)</li><li><a href="hot.xml">hot.xml</a> — trending story radar ({radar.count("<item>")} stories)</li></ul><p class="muted">Rebuilt by GitHub Actions; links point at the original sources.</p>\n'
    out.mkdir(parents=True, exist_ok=True)
    for name, body in {"feed.xml": news, "hot.xml": radar, "index.html": idx}.items():
        body = redact(body)
        if KEY and KEY in body:
            raise SystemExit(f"error: {name} would contain the API key")
        p = out / name
        if not p.exists() or p.read_bytes() != body.encode():
            p.write_bytes(body.encode())
            print(f"{name:11} updated")
        else:
            print(f"{name:11} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
