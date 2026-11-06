"""MCP server exposing data-collector tools. The orchestrator discovers these at runtime.

Sources: Google Play, Apple App Store, YouTube comments (all free) and the open web via Firecrawl.
Firecrawl calls go through a disk cache and a per-run credit budget.
"""
import asyncio, hashlib, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice
from typing import List, Optional
from urllib.parse import urlparse

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("collectors")
DEFAULT_KW = ["sip", "mutual fund", "autopay", "mandate", "nav", "xirr", "invest"]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "en-IN,en;q=0.9"}
# Sites that need a login / are covered by another tool / are explicitly out of scope (Reddit).
SKIP_DOMAINS = ("reddit.com", "youtube.com", "youtu.be", "play.google.com", "apps.apple.com",
                "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com")


def _log(*a):
    print(*a, file=sys.stderr)  # stdout belongs to the MCP protocol


def _hit(text, kws):
    t = text.lower()
    return any(k.lower() in t for k in (kws or DEFAULT_KW))


def _skip(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS)


# ---------- Google Play
def _play(app_id, keywords, count):
    from google_play_scraper import reviews, Sort
    rows, _ = reviews(app_id, lang="en", country="in", sort=Sort.NEWEST, count=min(count, 3000))
    out = []
    for r in rows:
        if not (r.get("content") and _hit(r["content"], keywords)):
            continue
        rid = r.get("reviewId")
        out.append({"source": "play_store", "site": "play.google.com", "text": r["content"], "rating": r["score"], "date": r["at"].strftime("%Y-%m-%d"),
                    "url": f"https://play.google.com/store/apps/details?id={app_id}" + (f"&reviewId={rid}" if rid else ""), "context": "Google Play review (India)"})
    return out


@mcp.tool()
async def play_store_reviews(app_id: str, keywords: Optional[List[str]] = None, count: int = 1500) -> str:
    """Fetch recent Google Play reviews (India) for an app id such as com.nextbillion.groww; keep only reviews containing any keyword."""
    return json.dumps(await asyncio.to_thread(_play, app_id, keywords, count))


# ---------- Apple App Store
def _app_store(app_id, keywords, pages):
    out = []
    for p in range(1, min(pages, 10) + 1):
        try:
            j = requests.get(f"https://itunes.apple.com/in/rss/customerreviews/page={p}/id={app_id}/sortby=mostrecent/json", timeout=20).json()
        except Exception:
            break
        ents = j.get("feed", {}).get("entry", [])
        for e in ents if isinstance(ents, list) else [ents]:
            if "im:rating" not in e:
                continue
            text = e["title"]["label"] + ". " + e["content"]["label"]
            if _hit(text, keywords):
                link = e.get("link")
                link = link[0] if isinstance(link, list) and link else link
                href = ((link or {}).get("attributes") or {}).get("href") if isinstance(link, dict) else None
                out.append({"source": "app_store", "site": "apps.apple.com", "text": text, "rating": int(e["im:rating"]["label"]), "date": e["updated"]["label"][:10],
                            "url": href or f"https://apps.apple.com/in/app/id{app_id}?see-all=reviews", "context": "App Store review (India)"})
    return out


@mcp.tool()
async def app_store_reviews(app_id: str, keywords: Optional[List[str]] = None, pages: int = 10) -> str:
    """Fetch recent Apple App Store reviews (India) via the public RSS feed for a numeric app id such as 1404871703; keep only reviews containing any keyword."""
    return json.dumps(await asyncio.to_thread(_app_store, app_id, keywords, pages))


# ---------- YouTube comments (no API key: yt-dlp for video search, youtube-comment-downloader for comments)
def _youtube(query, videos, per_video, keywords):
    import yt_dlp
    from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_POPULAR
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as y:
            info = y.extract_info(f"ytsearch{max(1, min(videos, 10))}:{query}", download=False)
        vids = [(e["id"], e.get("title", "")) for e in (info or {}).get("entries", []) if e and e.get("id")]
    except Exception as e:
        raise RuntimeError(f"YouTube search failed: {str(e)[:150]}")
    dl, out, failed = YoutubeCommentDownloader(), [], 0
    for vid, title in vids:
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            for c in islice(dl.get_comments_from_url(url, sort_by=SORT_BY_POPULAR), per_video):
                text = (c.get("text") or "").strip()
                if len(text.split()) < 5 or (keywords and not _hit(text, keywords)):
                    continue
                ts = c.get("time_parsed")
                date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else ""
                out.append({"source": "youtube", "site": "youtube.com", "text": text[:1200], "rating": None, "date": date, "url": url, "context": title})
        except Exception as e:
            failed += 1
            _log(f"youtube {vid} failed: {e}")
    if not out and failed and failed == len(vids):
        raise RuntimeError("YouTube comment download failed for every video (YouTube may be rate-limiting; retry later).")
    return out


@mcp.tool()
async def youtube_comments(query: str, max_videos: int = 5, comments_per_video: int = 120, keywords: Optional[List[str]] = None) -> str:
    """Search YouTube for videos matching the query (e.g. 'Groww SIP review', 'SIP band kar diya') and scrape the top comments of each video.
    Candid user reactions; no API key needed. Leave keywords empty unless needed: the videos are already topical."""
    return json.dumps(await asyncio.to_thread(_youtube, query, max_videos, min(comments_per_video, 300), keywords))


# ---------- Open web via Firecrawl (https://docs.firecrawl.dev)
# Credits (hosted plan): search = 2 per call (up to 10 results, no page content), scrape = 1 per page.
# So the cheap pattern is: search only -> rank the hits -> scrape just the best few -> use snippets for the rest.
FC_URL = os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev").rstrip("/")     # point this at a self-hosted instance to spend 0 credits
FC_VER = os.getenv("FIRECRAWL_API_VERSION", "v2")                                    # some self-hosted builds only expose v1
SELF_HOSTED = "api.firecrawl.dev" not in FC_URL
COUNT_CREDITS = (not SELF_HOSTED) or os.getenv("FIRECRAWL_COUNT_CREDITS") == "1"
BUDGET = int(os.getenv("FIRECRAWL_CREDIT_BUDGET", "40"))                              # per run; 0 = unlimited
PROFILE = os.getenv("FIRECRAWL_PROFILE", "lean").lower()                              # lean | standard | full
CONC = int(os.getenv("FIRECRAWL_CONCURRENCY", "2"))                                   # free plan allows ~2 concurrent requests
SCRAPE_TOP = {"lean": 6, "standard": 10, "full": 16}                                  # pages read in full per sweep
CACHE_DAYS = float(os.getenv("FIRECRAWL_CACHE_DAYS", "7"))
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "firecrawl")
_FC_SLOTS = threading.Semaphore(CONC)
_LOCK = threading.Lock()
_USAGE = {"credits": 0, "calls": 0, "searches": 0, "scrapes": 0, "cache_hits": 0}


def _cache_get(key):
    p = os.path.join(CACHE_DIR, key + ".json")
    try:
        if time.time() - os.path.getmtime(p) > CACHE_DAYS * 86400:
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _cache_put(key, j):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, key + ".json"), "w", encoding="utf-8") as f:
            json.dump(j, f)
    except Exception:
        pass


def _reserve(est):
    if not COUNT_CREDITS:
        return
    with _LOCK:
        if BUDGET and _USAGE["credits"] + est > BUDGET:
            raise RuntimeError(f"Firecrawl credit budget reached ({_USAGE['credits']} of {BUDGET} used this run). Raise FIRECRAWL_CREDIT_BUDGET in .env to allow more.")
        _USAGE["credits"] += est


def _refund(est):
    if COUNT_CREDITS:
        with _LOCK:
            _USAGE["credits"] -= est


def _fc(kind, payload, est, timeout=100):
    """One Firecrawl call: disk cache first (free), then budget check, then the request."""
    key = os.getenv("FIRECRAWL_API_KEY")
    if not key and not SELF_HOSTED:
        raise RuntimeError("FIRECRAWL_API_KEY is not set (add it to .env)")
    ck = hashlib.sha1((FC_URL + FC_VER + kind + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
    hit = _cache_get(ck)
    if hit is not None:
        with _LOCK:
            _USAGE["cache_hits"] += 1
        return hit
    _reserve(est)
    r = None
    try:
        for attempt in range(4):
            with _FC_SLOTS:
                r = requests.post(f"{FC_URL}/{FC_VER}/{kind}", json=payload, headers={"Authorization": f"Bearer {key or 'self-hosted'}"}, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(4 * (attempt + 1)); continue
            if r.status_code in (401, 403):
                raise RuntimeError("Firecrawl rejected the API key (HTTP %d)" % r.status_code)
            if r.status_code == 402:
                raise RuntimeError("Firecrawl credits exhausted (HTTP 402)")
            r.raise_for_status()
            j = r.json()
            _cache_put(ck, j)
            with _LOCK:
                _USAGE["calls"] += 1
                _USAGE["searches" if kind == "search" else "scrapes"] += 1
                real = j.get("creditsUsed")
                if COUNT_CREDITS and isinstance(real, (int, float)):
                    _USAGE["credits"] += real - est
            return j
        raise RuntimeError(f"Firecrawl busy or unavailable (last HTTP {r.status_code if r is not None else '?'})")
    except BaseException:
        _refund(est); raise


def _passages(md, kws, max_chunks):
    """Markdown -> short opinion-sized passages (~450 chars) so separate forum replies stay separate."""
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md or "")
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"https?://\S+", " ", md)
    paras = []
    for line in md.split("\n"):
        p = re.sub(r"^[#>*\-\s|]+", "", line).strip()
        if len(p.split()) >= 10 and p.count("|") < 3:
            paras.append(p)
    if kws:
        paras = [p for p in paras if _hit(p, kws)]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > 450:
            chunks.append(cur); cur = ""
        cur = (cur + " " + p).strip()
    if cur:
        chunks.append(cur)
    return [c[:1200] for c in chunks[:max_chunks]]


def _docs_from(items, kws, per_page=6):
    out = []
    for it in items:
        meta = it.get("metadata") or {}
        url = it.get("url") or meta.get("sourceURL") or meta.get("url") or ""
        if not url or _skip(url):
            continue
        ps = _passages(it.get("markdown") or "", kws, per_page)
        desc = it.get("description") or meta.get("description") or ""
        if not ps and len(desc.split()) >= 5 and _hit(desc, kws):
            ps = [desc]          # page not read: fall back to the search snippet
        host = (urlparse(url).hostname or "").removeprefix("www.")
        title = it.get("title") or meta.get("title") or ""
        out += [{"source": "web", "text": p, "rating": None, "date": (it.get("publishedDate") or "")[:10], "url": url, "site": host, "context": title[:140]} for p in ps]
    return out


def _search_items(query, limit, sources=("web",)):
    """Search only (2 credits, no page content): returns title / url / snippet for up to 10 results."""
    payload = {"query": query, "limit": max(1, min(limit, 10)), "timeout": 60000}
    if FC_VER == "v2":
        payload["sources"] = list(sources)
    j = _fc("search", payload, est=2)
    data = j.get("data") or {}
    return data if isinstance(data, list) else (data.get("web") or []) + (data.get("news") or [])


def _scrape_page(url):
    j = _fc("scrape", {"url": url, "formats": ["markdown"], "onlyMainContent": True}, est=1)
    return j.get("data") or {}


# Sites where people actually write about their own experience score higher when choosing which pages to read in full.
VOICE = {"mouthshut.com": 3, "consumercomplaints.in": 3, "trustpilot.com": 3, "quora.com": 2, "tradingqna.com": 2, "valuepickr.com": 2, "medium.com": 1}


def _score(it, kws):
    url = it.get("url") or ""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    text = ((it.get("title") or "") + " " + (it.get("description") or "")).lower()
    hits = sum(1 for k in (kws or DEFAULT_KW) if k.lower() in text)
    bonus = max([v for d, v in VOICE.items() if host == d or host.endswith("." + d)] or [0])
    return 2 * hits + bonus


def _read_pages(cands, kws, top_k, per_page=6):
    """cands = [(search_item, label)]. Reads the best `top_k` pages in full (1 credit each); every other hit still
    contributes its search snippet for free. Returns (docs, pages_read)."""
    seen, uniq = set(), []
    for it, label in cands:
        url = it.get("url") or (it.get("metadata") or {}).get("sourceURL") or ""
        if not url or url in seen or _skip(url):
            continue
        seen.add(url); uniq.append((it, label, url))
    uniq.sort(key=lambda x: -_score(x[0], kws))

    def grab(t):
        try:
            return t, _scrape_page(t[2]), None
        except Exception as e:
            return t, None, e
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        results = list(ex.map(grab, uniq[:max(0, top_k)]))
    docs, done, warned = [], set(), False
    for (it, label, url), page, err in results:
        if err is not None:
            if not warned:
                _log(f"page read failed ({url[:70]}): {str(err)[:140]}"); warned = True
            continue
        merged = {**it, "url": url, "markdown": page.get("markdown") or "", "metadata": {**(it.get("metadata") or {}), **(page.get("metadata") or {})}}
        got = _docs_from([merged], kws, per_page)
        if got:
            done.add(url)
        docs += [{**g, "via": label, "how": "page"} for g in got]
    for it, label, url in uniq:
        if url not in done:
            docs += [{**g, "via": label, "how": "snippet"} for g in _docs_from([it], kws)]
    return docs, len(done)


@mcp.tool()
async def web_search(query: str, max_results: int = 8, keywords: Optional[List[str]] = None, pages_to_read: int = 3) -> str:
    """Firecrawl search of the open web (blogs, forums, Q&A, review and news sites). Costs 2 credits for the search plus 1 credit for each page read in full
    (the best `pages_to_read`, default 3); the other results contribute only their search snippet. Reddit, social networks and app stores are excluded.
    Use focused queries such as 'groww sip stopped reasons forum' or 'sip pause kyun karte hain'. Credits are limited: call this sparingly."""
    def go():
        items = _search_items(query, min(max_results, 10))
        docs, _ = _read_pages([(it, "Web search") for it in items], keywords or DEFAULT_KW, min(pages_to_read, 6))
        return docs
    return json.dumps(await asyncio.to_thread(go))


@mcp.tool()
async def scrape_url(url: str, keywords: Optional[List[str]] = None, max_passages: int = 10) -> str:
    """Firecrawl scrape of one specific public page (1 credit): article, forum thread, review page. Handles JavaScript-rendered pages. Optional keywords keep only matching paragraphs."""
    if _skip(url):
        raise ValueError("this site is excluded (Reddit/social/app stores are covered by other tools or not allowed)")
    d = await asyncio.to_thread(_scrape_page, url)
    return json.dumps([{**x, "via": "Direct page", "how": "page"} for x in _docs_from([{**d, "url": url}], keywords, min(max_passages, 20))])


# One search per kind of place where Indian investors talk about an app. TradingQnA's own guidelines discourage support/complaint
# posts, so it sits in the 'full' profile only. Each tuple: (label, query, Firecrawl source, profiles that include it)
ALL, STD, FULL = ("lean", "standard", "full"), ("standard", "full"), ("full",)
SWEEP = [("Quora", "{t} site:quora.com", "web", ALL), ("MouthShut reviews", "{t} reviews site:mouthshut.com", "web", ALL),
         ("Trustpilot", "{t} site:trustpilot.com", "web", ALL), ("Hinglish discussions", "{t} sip band kyun kiya anubhav", "web", ALL),
         ("Open web", "{t} problems complaints honest review", "web", ALL),
         ("ConsumerComplaints", "{t} site:consumercomplaints.in", "web", STD), ("ValuePickr forum", "{t} site:valuepickr.com", "web", STD),
         ("TradingQnA forum", "{t} site:tradingqna.com", "web", FULL), ("Medium blogs", "{t} experience site:medium.com", "web", FULL),
         ("Moneycontrol", "{t} site:moneycontrol.com", "web", FULL), ("News", "{t}", "news", FULL)]


def _sweep(topic, kws, limit):
    kws = kws or DEFAULT_KW
    specs = [s for s in SWEEP if PROFILE in s[3]] or [s for s in SWEEP if "lean" in s[3]]
    errors = []

    def one(spec):
        label, q, src, _ = spec
        try:
            items = _search_items(q.format(t=topic), limit, (src,))
            _log(f"sweep {label}: {len(items)} results")
            return [(it, label) for it in items]
        except Exception as e:
            _log(f"sweep {label} failed: {e}"); errors.append(f"{label}: {e}")
            return []
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        cands = [c for part in ex.map(one, specs) for c in part]
    if not cands and errors:      # surface the real cause instead of a silent "0 items"
        raise RuntimeError(f"all {len(errors)} sweep searches failed. First error: {errors[0][:220]}")
    docs, read = _read_pages(cands, kws, SCRAPE_TOP.get(PROFILE, 6))
    _log(f"sweep done: profile={PROFILE}, {len(specs)} searches, {len(cands)} hits, {read} pages read in full, {len(docs)} passages")
    return docs


@mcp.tool()
async def source_sweep(topic: str, keywords: Optional[List[str]] = None, results_per_source: int = 8) -> str:
    """Broad multi-source sweep with Firecrawl: one search each on review sites (MouthShut, Trustpilot), Q&A (Quora), Hinglish discussions and the open web
    (more forums/blogs when FIRECRAWL_PROFILE is standard or full). Reads only the best-ranked pages in full and uses snippets for the rest, to save credits.
    Topic example: 'Groww SIP'. Call this ONCE early for wide coverage."""
    return json.dumps(await asyncio.to_thread(_sweep, topic, keywords, min(results_per_source, 10)))


@mcp.tool()
async def firecrawl_usage() -> str:
    """Internal: Firecrawl credits spent so far in this run."""
    return json.dumps({**_USAGE, "budget": BUDGET, "profile": PROFILE, "self_hosted": SELF_HOSTED, "counted": COUNT_CREDITS})


if __name__ == "__main__":
    mcp.run()
