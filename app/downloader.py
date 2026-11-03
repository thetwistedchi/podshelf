"""Persistent download queue. Every queued episode lives in the database, so
nothing is lost on restart, and each episode is retried with backoff instead
of silently dropping out (the 'only 1 or 2 episodes' problem)."""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from . import config, db, feeds, naming, postprocess, tagger

log = logging.getLogger("podshelf.downloader")

progress: dict[int, dict] = {}  # episode_id -> {"done": bytes, "total": bytes, "speed": B/s}
_stop = threading.Event()
_cancel: set[int] = set()

AUDIO_EXT = {".mp3", ".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".opus", ".oga", ".flac", ".wav"}
TYPE_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
            "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/flac": ".flac",
            "video/mp4": ".mp4", "audio/wav": ".wav", "audio/x-wav": ".wav"}


# ---------------------------------------------------------------- bandwidth + window
class Limiter:
    """Shared token bucket: all workers together stay under the configured KB/s."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = 0.0
        self.last = time.monotonic()
        self.rate = 0
        self.rate_read = 0.0

    def _rate(self):
        now = time.monotonic()
        if now - self.rate_read > 5:
            try:
                self.rate = max(0, int(db.get_setting("bandwidth_kbps") or 0)) * 1024
            except ValueError:
                self.rate = 0
            self.rate_read = now
        return self.rate

    def consume(self, n: int):
        rate = self._rate()
        if not rate:
            return
        with self.lock:
            now = time.monotonic()
            self.tokens = min(rate, self.tokens + (now - self.last) * rate)  # max 1 s burst
            self.last = now
            self.tokens -= n
            wait = -self.tokens / rate if self.tokens < 0 else 0
        if wait:
            time.sleep(wait)


limiter = Limiter()


def in_window(now: datetime | None = None) -> bool:
    s, e = db.get_setting("window_start"), db.get_setting("window_end")
    if s in (None, "") or e in (None, ""):
        return True
    try:
        s, e = int(s), int(e)
    except ValueError:
        return True
    h = (now or datetime.now()).hour
    if s == e:
        return True
    return s <= h < e if s < e else (h >= s or h < e)


# ---------------------------------------------------------------- helpers
def _sniff_ext(path: str, fallback: str) -> str:
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return fallback
    if head[:3] == b"ID3":
        return fallback if fallback in (".mp3", ".aac") else ".mp3"
    if head[4:8] == b"ftyp":
        return ".mp4" if fallback == ".mp4" else ".m4a"
    if head[:4] == b"OggS":
        return ".opus" if b"OpusHead" in head else ".ogg"
    if head[:4] == b"fLaC":
        return ".flac"
    if head[:4] == b"RIFF":
        return ".wav"
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return ".aac"
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return ".mp3"
    return fallback


def _guess_ext(url: str, ctype: str | None) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in AUDIO_EXT:
        return ext
    return TYPE_EXT.get((ctype or "").split(";")[0].strip().lower(), ".mp3")


def episode_number(ep) -> int:
    """User-set number, else chronological position among all known episodes of the feed."""
    if ep["number_override"]:
        return ep["number_override"]
    row = db.one(
        "SELECT COUNT(*) AS n FROM episodes WHERE podcast_id=? AND "
        "(COALESCE(pub_date,'') < COALESCE(?, '') OR (COALESCE(pub_date,'') = COALESCE(?, '') AND id < ?))",
        (ep["podcast_id"], ep["pub_date"], ep["pub_date"], ep["id"]),
    )
    return row["n"] + 1


def ep_title(ep) -> str:
    return (ep["title_override"] or ep["title"] or "").strip()


def _is_bonus(ep) -> bool:
    return (ep["episode_type"] or "full") in ("bonus", "trailer")


def subfolder(p, ep) -> str:
    mode = p["split_mode"] or "none"
    if mode in ("bonus", "both") and _is_bonus(ep):
        return "Bonus"
    if mode in ("season", "both") and ep["season"]:
        return f"Season {ep['season']:02d}"
    return ""


def album_for(p, ep) -> str:
    """Album tag. Splitting gives Plex/Emby one album per season or for bonus episodes."""
    base = feeds.ptitle(p)
    sub = subfolder(p, ep)
    if sub == "Bonus":
        return f"{base} - Bonus"
    if sub:
        return f"{base} - Season {ep['season']}"
    return base


def target_path(p, ep, ext: str) -> str:
    base = naming.episode_basename(db.get_setting("episode_format"), feeds.ptitle(p), ep_title(ep),
                                   ep["pub_date"], episode_number(ep), p["author"] or "")
    return os.path.join(feeds.podcast_dir(p), subfolder(p, ep), base + ext)


def _cover_bytes(p, ep, client) -> bytes | None:
    folder_cover = os.path.join(feeds.podcast_dir(p), "cover.jpg")
    use_podcast = db.get_setting("force_podcast_cover") == "1"
    if ep["image_url"] and not use_podcast and ep["image_url"] != p["image_url"]:
        try:
            r = client.get(ep["image_url"], timeout=30)
            r.raise_for_status()
            if len(r.content) > 500:
                return feeds._to_jpeg(r.content, 1000)
        except Exception:
            pass
    if os.path.isfile(folder_cover):
        with open(folder_cover, "rb") as fh:
            return fh.read()
    return None


def tag_episode(ep, p=None, client=None):
    p = p or db.one("SELECT * FROM podcasts WHERE id=?", (ep["podcast_id"],))
    own = client is None
    client = client or feeds.http_client(30)
    try:
        tagger.tag_file(
            ep["file_path"],
            album=album_for(p, ep),
            title=ep_title(ep),
            track=episode_number(ep),
            year=(ep["pub_date"] or "")[:4] or None,
            description=ep["description"] or "",
            cover=_cover_bytes(p, ep, client),
            strip_date=db.get_setting("strip_date_tag") == "1",
        )
    finally:
        if own:
            client.close()
    _set_mtime(ep["file_path"], ep["pub_date"])


def _set_mtime(path, pub_date):
    if not pub_date:
        return
    try:
        ts = datetime.fromisoformat(pub_date).timestamp()
        os.utime(path, (ts, ts))
    except (ValueError, OSError):
        pass


def _subfolder_cover(p, path):
    """Season/Bonus folders get a copy of cover.jpg so Plex/Emby show folder art there too."""
    folder = os.path.dirname(path)
    src = os.path.join(feeds.podcast_dir(p), "cover.jpg")
    dst = os.path.join(folder, "cover.jpg")
    if folder != feeds.podcast_dir(p) and os.path.isfile(src) and not os.path.isfile(dst):
        try:
            import shutil
            shutil.copyfile(src, dst)
        except OSError:
            pass


def organize(eid: int):
    """Move the file to where the current naming/folder rules want it."""
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    if not ep or not ep["file_path"] or not os.path.isfile(ep["file_path"]):
        return
    p = db.one("SELECT * FROM podcasts WHERE id=?", (ep["podcast_id"],))
    ext = os.path.splitext(ep["file_path"])[1].lower()
    target = target_path(p, ep, ext)
    if target == ep["file_path"]:
        return
    if os.path.exists(target):
        target = os.path.splitext(target)[0] + f" ({eid}){ext}"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    old_dir = os.path.dirname(ep["file_path"])
    os.replace(ep["file_path"], target)
    with db.tx() as c:
        c.execute("UPDATE episodes SET file_path=? WHERE id=?", (target, eid))
    _subfolder_cover(p, target)
    if old_dir != feeds.podcast_dir(p):
        try:
            leftover = os.listdir(old_dir)
            if leftover == ["cover.jpg"]:
                os.remove(os.path.join(old_dir, "cover.jpg"))
            os.rmdir(old_dir)  # remove empty season/bonus folder
        except OSError:
            pass


# ---------------------------------------------------------------- post-processing
def _chapters_for(ep, client, auth_ok) -> list[dict]:
    chapters = postprocess.loads(ep["chapters"])  # feed chapters always refer to the original file
    if not chapters and ep["chapters_url"]:
        try:
            r = client.get(ep["chapters_url"], timeout=30)
            r.raise_for_status()
            chapters = postprocess.chapters_from_json(r.json())
        except Exception as e:
            log.info("chapters for %s not loaded: %s", ep["title"], e)
    if not chapters and ep["file_path"]:
        chapters = postprocess.chapters_from_file(ep["file_path"])
    return chapters


def process(eid: int, client) -> bool:
    """Length check, ad skipping, chapters, tags. Returns False if the episode was rejected."""
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    p = db.one("SELECT * FROM podcasts WHERE id=?", (ep["podcast_id"],))
    path = ep["file_path"]
    length = postprocess.audio_length(path)

    # length rules only apply to automatic downloads, not to ones you picked by hand
    if length and not ep["pinned"]:
        reason = None
        if p["min_minutes"] and length < p["min_minutes"] * 60:
            reason = f"too short ({length // 60} min)"
        elif p["max_minutes"] and length > p["max_minutes"] * 60:
            reason = f"too long ({length // 60} min)"
        if reason:
            delete_file(eid, status="skipped")
            with db.tx() as c:
                c.execute("UPDATE episodes SET skip_reason=? WHERE id=?", (reason, eid))
            log.info("rejected %s: %s", ep["title"], reason)
            return False

    chapters = _chapters_for(ep, client, True)
    cut_secs = 0
    if length and (p["trim_start"] or p["trim_end"] or p["cut_ad_chapters"]):
        keywords = db.get_setting("ad_keywords")
        keep = postprocess.plan_cuts(length, chapters, p["trim_start"], p["trim_end"],
                                     bool(p["cut_ad_chapters"]), keywords)
        if keep and postprocess.cut(path, keep):
            is_ad = postprocess.ad_matcher(keywords) if p["cut_ad_chapters"] else (lambda t: False)
            chapters = postprocess.shift_chapters(chapters, keep, is_ad)
            new_len = postprocess.audio_length(path) or length
            cut_secs = max(0, length - new_len)
            length = new_len
    try:
        postprocess.embed_chapters(path, chapters, length)
    except Exception as e:
        log.warning("embedding chapters into %s failed: %s", path, e)
    with db.tx() as c:
        c.execute("UPDATE episodes SET file_chapters=?, length_secs=?, cut_secs=?, file_size=? WHERE id=?",
                  (postprocess.dumps(chapters), length, cut_secs, os.path.getsize(path), eid))
    tag_episode(db.one("SELECT * FROM episodes WHERE id=?", (eid,)), p, client)
    return True


# ---------------------------------------------------------------- queue
def _claim():
    now = feeds.now_iso()
    window_ok = in_window()
    with db.tx() as c:
        row = c.execute(
            "SELECT e.id FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
            "WHERE e.status='queued' AND (e.next_try IS NULL OR e.next_try<=?) "
            + ("" if window_ok else "AND e.pinned=1 ") +  # outside the window only manual downloads run
            "ORDER BY e.pinned DESC, e.pub_date DESC LIMIT 1", (now,)).fetchone()
        if not row:
            return None
        c.execute("UPDATE episodes SET status='downloading', error=NULL WHERE id=?", (row["id"],))
    return row["id"]


def _worker(idx: int):
    client = feeds.http_client(config.HTTP_TIMEOUT)
    while not _stop.is_set():
        try:
            limit = max(1, min(config.MAX_WORKERS, int(db.get_setting("parallel_downloads") or 3)))
        except ValueError:
            limit = 3
        if idx >= limit:
            _stop.wait(15)
            continue
        eid = _claim()
        if eid is None:
            feeds.wake_downloads.wait(10)
            feeds.wake_downloads.clear()
            continue
        try:
            download(eid, client)
        except Exception as e:
            log.exception("download %s crashed", eid)
            _fail(eid, str(e))


def _fail(eid, msg):
    ep = db.one("SELECT attempts FROM episodes WHERE id=?", (eid,))
    attempts = (ep["attempts"] if ep else 0) + 1
    if attempts >= config.MAX_ATTEMPTS:
        status, next_try = "failed", None
    else:
        status = "queued"
        delay = min(3600, 60 * 2 ** (attempts - 1))  # 1, 2, 4, 8 ... minutes
        next_try = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    with db.tx() as c:
        c.execute("UPDATE episodes SET status=?, attempts=?, next_try=?, error=? WHERE id=?",
                  (status, attempts, next_try, msg[:500], eid))
    log.warning("episode %s: %s (attempt %d, %s)", eid, msg, attempts, status)


def download(eid: int, client: httpx.Client):
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    p = db.one("SELECT * FROM podcasts WHERE id=?", (ep["podcast_id"],))
    folder = feeds.podcast_dir(p)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".podshelf-{eid}.part")
    # premium feeds: credentials only go to the feed's own host
    auth = feeds.podcast_auth(p) if feeds.same_host(ep["enclosure_url"], p["feed_url"]) else None
    progress[eid] = {"done": 0, "total": 0, "speed": 0}
    try:
        with client.stream("GET", ep["enclosure_url"], **({"auth": auth} if auth else {})) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            progress[eid]["total"] = total
            ext = _guess_ext(str(r.url), r.headers.get("content-type") or ep["enclosure_type"])
            done, t0 = 0, time.monotonic()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(64 * 1024):
                    if eid in _cancel:
                        raise InterruptedError("cancelled")
                    limiter.consume(len(chunk))
                    fh.write(chunk)
                    done += len(chunk)
                    el = max(0.001, time.monotonic() - t0)
                    progress[eid].update(done=done, speed=int(done / el))
        if total and done < total:
            raise IOError(f"incomplete download ({done} of {total} bytes)")
        if done < 10_000:
            raise IOError(f"file too small ({done} bytes), probably not audio")
        ext = _sniff_ext(tmp, ext)
        final = target_path(p, ep, ext)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        if os.path.exists(final) and not _same_episode(final, ep):
            final = os.path.splitext(final)[0] + f" ({eid}){ext}"
        os.replace(tmp, final)
        _subfolder_cover(p, final)
    except InterruptedError:
        _cleanup(tmp)
        _cancel.discard(eid)
        with db.tx() as c:
            c.execute("UPDATE episodes SET status='new', error='cancelled' WHERE id=?", (eid,))
        return
    except Exception as e:
        _cleanup(tmp)
        _fail(eid, f"{type(e).__name__}: {e}")
        return
    finally:
        progress.pop(eid, None)

    with db.tx() as c:
        c.execute("UPDATE episodes SET status='done', file_path=?, file_size=?, downloaded_at=?, "
                  "error=NULL, next_try=NULL, skip_reason=NULL WHERE id=?",
                  (final, os.path.getsize(final), feeds.now_iso(), eid))
    try:
        if not process(eid, client):
            return
    except Exception as e:  # keep the file even if post-processing fails
        log.warning("post-processing %s failed: %s", final, e)
        with db.tx() as c:
            c.execute("UPDATE episodes SET error=? WHERE id=?", (f"post-processing: {e}"[:500], eid))
    log.info("downloaded %s / %s", feeds.ptitle(p), ep["title"])
    apply_retention(p["id"])


def _same_episode(path, ep):
    other = db.one("SELECT id FROM episodes WHERE file_path=?", (path,))
    return other is None or other["id"] == ep["id"]


def _cleanup(path):
    try:
        os.remove(path)
    except OSError:
        pass


def cancel(eid: int):
    if eid in progress:
        _cancel.add(eid)


# ---------------------------------------------------------------- retention + file ops
def apply_retention(pid: int):
    p = db.one("SELECT keep_count, keep_days, delete_played_days FROM podcasts WHERE id=?", (pid,))
    if not p:
        return
    rows = db.query(
        "SELECT id, pub_date, played, played_at FROM episodes WHERE podcast_id=? AND status='done' "
        "AND pinned=0 ORDER BY pub_date DESC, id DESC", (pid,))
    now = datetime.now(timezone.utc)
    doomed = set()
    if p["keep_count"] > 0:
        doomed.update(r["id"] for r in rows[p["keep_count"]:])
    if p["keep_days"] > 0:
        limit = now - timedelta(days=p["keep_days"])
        doomed.update(r["id"] for r in rows if r["pub_date"] and _dt(r["pub_date"]) < limit)
    if p["delete_played_days"] >= 0:
        limit = now - timedelta(days=p["delete_played_days"])
        doomed.update(r["id"] for r in rows
                      if r["played"] and (not r["played_at"] or _dt(r["played_at"]) <= limit))
    for eid in doomed:
        delete_file(eid, status="deleted")


def _dt(iso: str) -> datetime:
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def apply_retention_all():
    for r in db.query("SELECT id FROM podcasts"):
        apply_retention(r["id"])


def delete_file(eid: int, status="deleted"):
    ep = db.one("SELECT file_path FROM episodes WHERE id=?", (eid,))
    if ep and ep["file_path"]:
        _cleanup(ep["file_path"])
    with db.tx() as c:
        c.execute("UPDATE episodes SET status=?, file_path=NULL, file_size=NULL, pinned=0 WHERE id=?",
                  (status, eid))


def queue(eid: int, pinned=True):
    with db.tx() as c:
        c.execute("UPDATE episodes SET status='queued', attempts=0, next_try=NULL, error=NULL, "
                  "skip_reason=NULL, pinned=? WHERE id=? AND status NOT IN ('done','downloading')",
                  (1 if pinned else 0, eid))
    feeds.wake_downloads.set()


def retag_podcast(pid: int):
    """Re-apply naming, folders, tags and covers to every downloaded file."""
    p = db.one("SELECT * FROM podcasts WHERE id=?", (pid,))
    feeds.save_cover(p)
    n = 0
    with feeds.http_client(30) as client:
        for ep in db.query("SELECT id FROM episodes WHERE podcast_id=? AND status='done'", (pid,)):
            try:
                retag_episode(ep["id"], client)
                n += 1
            except Exception as e:
                log.warning("retag episode %s failed: %s", ep["id"], e)
    return n


def retag_episode(eid: int, client=None):
    organize(eid)
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    if ep and ep["file_path"] and os.path.isfile(ep["file_path"]):
        tag_episode(ep, None, client)


def adopt(ids):
    """Take over files that were already on disk: rename to our scheme and tag."""
    pid = None
    with feeds.http_client(30) as client:
        for eid in ids:
            ep = db.one("SELECT podcast_id FROM episodes WHERE id=?", (eid,))
            pid = ep["podcast_id"]
            try:
                retag_episode(eid, client)
            except Exception as e:
                log.warning("adopting episode %s failed: %s", eid, e)
    if pid:
        apply_retention(pid)


def verify_files():
    """Mark episodes whose file disappeared (deleted outside the app)."""
    # never act when the share is not mounted / empty
    if not os.path.isdir(config.PODCAST_DIR) or not os.listdir(config.PODCAST_DIR):
        return
    for ep in db.query("SELECT id, file_path FROM episodes WHERE status='done'"):
        if not ep["file_path"] or not os.path.isfile(ep["file_path"]):
            with db.tx() as c:
                c.execute("UPDATE episodes SET status='deleted', file_path=NULL WHERE id=?", (ep["id"],))


def start():
    for i in range(config.MAX_WORKERS):
        threading.Thread(target=_worker, args=(i,), daemon=True, name=f"dl-{i}").start()
