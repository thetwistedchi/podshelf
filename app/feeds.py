import calendar
import hashlib
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

import feedparser
import httpx

from . import config, db, naming, postprocess

log = logging.getLogger("podshelf.feeds")
_refresh_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()
wake_downloads = threading.Event()


def http_client(timeout=None, auth=None) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout or config.HTTP_TIMEOUT, connect=20),
        headers={"User-Agent": config.USER_AGENT},
        auth=auth,
    )


def podcast_auth(p):
    """HTTP basic auth for premium feeds, or None."""
    if p and p["auth_user"]:
        return httpx.BasicAuth(p["auth_user"], p["auth_pass"] or "")
    return None


def same_host(a: str, b: str) -> bool:
    return urlparse(a).hostname == urlparse(b).hostname


def ptitle(p) -> str:
    """Podcast title as shown and tagged (user override wins)."""
    return (p["title_override"] or p["title"] or "").strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- parsing
def _iso(struct) -> str | None:
    if not struct:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(struct), timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, ValueError, TypeError):
        return None


def _enclosure(entry):
    for enc in entry.get("enclosures", []) or []:
        url = enc.get("href") or enc.get("url")
        typ = (enc.get("type") or "").lower()
        if url and (typ.startswith("audio") or typ.startswith("video") or not typ):
            return url, typ
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"], (link.get("type") or "").lower()
    return None, None


def _image(obj):
    img = obj.get("image")
    if isinstance(img, dict) and img.get("href"):
        return img["href"]
    if obj.get("itunes_image"):
        return obj["itunes_image"]
    return None


NS_PSC = "{http://podlove.org/simple-chapters}"
NS_P20 = "{https://podcastindex.org/namespace/1.0}"


def _raw_chapters(content: bytes) -> dict:
    """{guid or enclosure url: {"chapters": [...], "url": str}} straight from the XML,
    because feedparser does not expose Podlove / Podcasting 2.0 chapters reliably."""
    out = {}
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        guid = (item.findtext("guid") or "").strip()
        enc = item.find("enclosure")
        key = guid or (enc.get("url") if enc is not None else "")
        data = {}
        psc = item.find(NS_PSC + "chapters")
        if psc is not None:
            chs = []
            for ch in psc.findall(NS_PSC + "chapter"):
                t = postprocess.parse_time(ch.get("start"))
                if t is not None:
                    chs.append({"start": t, "title": (ch.get("title") or "").strip()})
            if chs:
                data["chapters"] = sorted(chs, key=lambda c: c["start"])
        p20 = item.find(NS_P20 + "chapters")
        if p20 is not None and p20.get("url"):
            data["url"] = p20.get("url")
        if data and key:
            out[key] = data
    return out


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_feed(content: bytes, url: str):
    f = feedparser.parse(content)
    raw_ch = _raw_chapters(content)
    if f.bozo and not f.entries and not f.feed.get("title"):
        raise ValueError(f"Not a valid feed: {f.get('bozo_exception')}")
    feed = f.feed
    meta = {
        "title": (feed.get("title") or url).strip(),
        "author": (feed.get("author") or feed.get("itunes_author") or "").strip(),
        "description": feed.get("subtitle") or feed.get("summary") or "",
        "image_url": _image(feed),
    }
    episodes = []
    for e in f.entries:
        enc_url, enc_type = _enclosure(e)
        if not enc_url:
            continue
        pub = _iso(e.get("published_parsed")) or _iso(e.get("updated_parsed"))
        guid = (e.get("id") or e.get("guid") or enc_url).strip()
        ch = raw_ch.get(guid) or raw_ch.get(enc_url) or {}
        episodes.append({
            "guid": guid,
            "title": (e.get("title") or "Untitled").strip(),
            "description": e.get("summary") or "",
            "pub_date": pub,
            "enclosure_url": enc_url,
            "enclosure_type": enc_type,
            "image_url": _image(e),
            "duration": str(e.get("itunes_duration") or ""),
            "season": _int(e.get("itunes_season")),
            "episode_type": (e.get("itunes_episodetype") or "full").strip().lower(),
            "chapters": postprocess.dumps(ch.get("chapters")),
            "chapters_url": ch.get("url"),
        })
    return meta, episodes


def fetch_feed(url: str, auth=None):
    with http_client(auth=auth) as c:
        r = c.get(url)
        r.raise_for_status()
        return parse_feed(r.content, url)


# ---------------------------------------------------------------- covers
def podcast_dir(p) -> str:
    return os.path.join(config.PODCAST_DIR, p["folder"])


def save_cover(p, client=None):
    if not p["image_url"] or not p["folder"]:
        return
    path = os.path.join(podcast_dir(p), "cover.jpg")
    if p["cover_saved"] == p["image_url"] and os.path.isfile(path):
        return
    own = client is None
    client = client or http_client(30)
    try:
        auth = podcast_auth(p) if same_host(p["image_url"], p["feed_url"]) else None
        r = client.get(p["image_url"], auth=auth) if auth else client.get(p["image_url"])
        r.raise_for_status()
        data = r.content
        if len(data) < 500:
            raise ValueError("image too small")
        os.makedirs(podcast_dir(p), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(_to_jpeg(data))
        os.replace(tmp, path)
        with db.tx() as c:
            c.execute("UPDATE podcasts SET cover_saved=? WHERE id=?", (p["image_url"], p["id"]))
    except Exception as e:  # cover is nice to have, never fatal
        log.warning("cover for %s failed: %s", ptitle(p), e)
    finally:
        if own:
            client.close()


def _to_jpeg(data: bytes, max_px: int = 1400) -> bytes:
    """Normalise artwork to a reasonably sized JPEG (Plex/Emby like that best)."""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(data))
        im = im.convert("RGB")
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px))
        out = BytesIO()
        im.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return data


def cached_image(url: str) -> str | None:
    """Download an image once into /data/cache, return local path."""
    if not url:
        return None
    cache = os.path.join(config.DATA_DIR, "cache")
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, hashlib.sha1(url.encode()).hexdigest() + ".jpg")
    if os.path.isfile(path):
        return path
    try:
        with http_client(30) as c:
            r = c.get(url)
            r.raise_for_status()
        with open(path + ".part", "wb") as fh:
            fh.write(_to_jpeg(r.content, 600))
        os.replace(path + ".part", path)
        return path
    except Exception:
        return None


# ---------------------------------------------------------------- podcasts
def add_podcast(feed_url: str, keep=None, initial=None, check_hours=None, max_new=None,
                auto=True) -> int:
    feed_url = feed_url.strip()
    existing = db.one("SELECT id FROM podcasts WHERE feed_url=?", (feed_url,))
    if existing:
        return existing["id"]
    g = db.get_setting
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO podcasts(feed_url, title, keep_count, initial_count, check_hours, max_new, auto_download) "
            "VALUES (?,?,?,?,?,?,?)",
            (feed_url, feed_url,
             int(keep if keep is not None else g("default_keep")),
             int(initial if initial is not None else g("default_initial")),
             int(check_hours if check_hours is not None else g("default_check_hours")),
             int(max_new if max_new is not None else g("default_max_new")),
             1 if auto else 0),
        )
        return cur.lastrowid


_pool = None


def refresh_async(pid: int):
    global _pool
    if _pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="refresh")
    _pool.submit(refresh_podcast, pid)


def _lock_for(pid):
    with _locks_guard:
        return _refresh_locks.setdefault(pid, threading.Lock())


def is_refreshing(pid) -> bool:
    lk = _refresh_locks.get(pid)
    return bool(lk and lk.locked())


def refresh_podcast(pid: int):
    lock = _lock_for(pid)
    if not lock.acquire(blocking=False):
        return  # already running
    try:
        _refresh(pid)
    except Exception as e:
        log.exception("refresh %s failed", pid)
        with db.tx() as c:
            c.execute("UPDATE podcasts SET last_checked=?, last_error=? WHERE id=?",
                      (now_iso(), str(e)[:500], pid))
    finally:
        lock.release()


def filter_reason(p, e) -> str | None:
    """Why an episode should not be auto-downloaded, or None."""
    title = e.get("title") or ""
    inc = [w.strip() for w in (p["include_filter"] or "").split(",") if w.strip()]
    exc = [w.strip() for w in (p["exclude_filter"] or "").split(",") if w.strip()]
    if inc and not any(_matches(w, title) for w in inc):
        return "filtered: title does not match include filter"
    for w in exc:
        if _matches(w, title):
            return f"filtered: title contains “{w}”"
    secs = postprocess.parse_time(e.get("duration"))
    if secs:
        if p["min_minutes"] and secs < p["min_minutes"] * 60:
            return f"too short ({int(secs // 60)} min)"
        if p["max_minutes"] and secs > p["max_minutes"] * 60:
            return f"too long ({int(secs // 60)} min)"
    if p["keep_days"] and e.get("pub_date"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(e["pub_date"])
            if age.days >= p["keep_days"]:
                return f"older than {p['keep_days']} days"
        except ValueError:
            pass
    return None


def _matches(word: str, title: str) -> bool:
    if word.startswith("re:"):
        try:
            return re.search(word[3:], title, re.I) is not None
        except re.error:
            return False
    return word.lower() in title.lower()


def _duplicate_of(c, pid, title) -> int | None:
    n = _norm(title)
    if len(n) < 8:
        return None
    for r in c.execute("SELECT id, title FROM episodes WHERE podcast_id=?", (pid,)):
        if _norm(r["title"]) == n:
            return r["id"]
    return None


EP_COLS = ("guid", "title", "description", "pub_date", "enclosure_url", "enclosure_type",
           "image_url", "duration", "season", "episode_type", "chapters", "chapters_url")


def _refresh(pid: int):
    p = db.one("SELECT * FROM podcasts WHERE id=?", (pid,))
    if not p:
        return
    meta, episodes = fetch_feed(p["feed_url"], podcast_auth(p))
    folder = p["folder"] or _unique_folder(
        naming.podcast_folder(db.get_setting("podcast_format"), p["title_override"] or meta["title"],
                              meta["author"]), pid)

    newest_first = sorted(episodes, key=lambda e: e["pub_date"] or "", reverse=True)
    queued_now = 0
    # first run: adopt files that already exist in the folder (e.g. ABS downloads)
    existing = _existing_files(os.path.join(config.PODCAST_DIR, folder)) if not p["initialized"] else {}
    adopted = []
    with db.tx() as c:
        c.execute(
            "UPDATE podcasts SET title=?, author=?, description=?, image_url=COALESCE(?, image_url), "
            "folder=?, last_checked=?, last_error=NULL WHERE id=?",
            (meta["title"], meta["author"], meta["description"], meta["image_url"], folder, now_iso(), pid),
        )
        first_run = not p["initialized"]
        new_budget = p["initial_count"] if first_run else p["max_new"]
        for e in newest_first:
            row = c.execute("SELECT id FROM episodes WHERE podcast_id=? AND guid=?",
                            (pid, e["guid"])).fetchone()
            if row:
                c.execute(
                    "UPDATE episodes SET title=?, description=?, pub_date=COALESCE(?, pub_date), "
                    "enclosure_url=?, enclosure_type=?, image_url=?, duration=?, season=?, episode_type=?, "
                    "chapters=COALESCE(?, chapters), chapters_url=COALESCE(?, chapters_url) WHERE id=?",
                    (e["title"], e["description"], e["pub_date"], e["enclosure_url"], e["enclosure_type"],
                     e["image_url"], e["duration"], e["season"], e["episode_type"], e["chapters"],
                     e["chapters_url"], row["id"]),
                )
                continue
            values = [e[k] for k in EP_COLS]
            cols = ", ".join(EP_COLS)
            marks = ",".join("?" * len(EP_COLS))
            match = existing.get(_norm(e["title"])) if existing else None
            if match:
                for k in [k for k, v in existing.items() if v == match]:
                    del existing[k]
                cur = c.execute(
                    f"INSERT INTO episodes(podcast_id, {cols}, status, file_path, file_size, downloaded_at) "
                    f"VALUES (?, {marks}, 'done', ?, ?, ?)",
                    [pid, *values, match, os.path.getsize(match), now_iso()])
                adopted.append(cur.lastrowid)
                queued_now += 1  # counts toward the initial budget
                continue
            reason = None
            dup = _duplicate_of(c, pid, e["title"])
            if dup:
                reason = f"duplicate of episode #{dup}"
            elif p["auto_download"]:
                reason = filter_reason(p, e)
            if p["auto_download"] and not reason and (new_budget <= 0 or queued_now < new_budget):
                status = "queued"
                queued_now += 1
            else:
                # first run: older backlog is skipped; later: just listed as new
                status = "skipped" if (first_run or reason) else "new"
            c.execute(
                f"INSERT INTO episodes(podcast_id, {cols}, status, skip_reason) VALUES (?, {marks}, ?, ?)",
                [pid, *values, status, reason])
        c.execute("UPDATE podcasts SET initialized=1 WHERE id=?", (pid,))

    os.makedirs(os.path.join(config.PODCAST_DIR, folder), exist_ok=True)
    save_cover(db.one("SELECT * FROM podcasts WHERE id=?", (pid,)))
    if adopted:
        from . import downloader  # local import, avoids a cycle
        downloader.adopt(adopted)
        queued_now -= len(adopted)
    if queued_now:
        wake_downloads.set()
    log.info("refreshed %s: %d episodes in feed, %d queued", meta["title"], len(episodes), queued_now)


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _existing_files(folder: str) -> dict:
    """{normalised name: path} for audio files already in the folder."""
    out = {}
    if not os.path.isdir(folder):
        return out
    from .downloader import AUDIO_EXT
    for f in os.listdir(folder):
        stem, ext = os.path.splitext(f)
        if ext.lower() in AUDIO_EXT and not f.startswith("."):
            path = os.path.join(folder, f)
            if not db.one("SELECT 1 FROM episodes WHERE file_path=?", (path,)):
                out[_norm(stem)] = path
                # also match our own "date - title" scheme
                if " - " in stem:
                    out.setdefault(_norm(stem.split(" - ", 1)[1]), path)
    return out


def _unique_folder(name: str, pid: int) -> str:
    candidate, n = name, 2
    while db.one("SELECT 1 FROM podcasts WHERE folder=? AND id<>?", (candidate, pid)):
        candidate = f"{name} ({n})"
        n += 1
    return candidate


# ---------------------------------------------------------------- search + OPML
def itunes_search(term: str):
    with http_client(20) as c:
        r = c.get("https://itunes.apple.com/search",
                  params={"media": "podcast", "term": term, "limit": 25})
        r.raise_for_status()
        res = r.json().get("results", [])
    return [{
        "title": x.get("collectionName"),
        "author": x.get("artistName"),
        "feed_url": x.get("feedUrl"),
        "image": x.get("artworkUrl600") or x.get("artworkUrl100"),
        "genre": x.get("primaryGenreName"),
        "count": x.get("trackCount"),
    } for x in res if x.get("feedUrl")]


def parse_opml(data: bytes) -> list[str]:
    root = ET.fromstring(data)
    urls = []
    for o in root.iter("outline"):
        u = o.get("xmlUrl") or o.get("xmlurl") or o.get("url")
        if u and u not in urls:
            urls.append(u.strip())
    return urls


def export_opml() -> str:
    rows = db.query("SELECT title, feed_url FROM podcasts ORDER BY title COLLATE NOCASE")
    body = "\n".join(
        f'    <outline type="rss" text={quoteattr(r["title"] or "")} title={quoteattr(r["title"] or "")} '
        f'xmlUrl={quoteattr(r["feed_url"])}/>' for r in rows)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">\n'
            f'  <head><title>{escape("Podshelf export")}</title></head>\n'
            f'  <body>\n{body}\n  </body>\n</opml>\n')
