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

from . import config, db, feeds, naming, tagger

log = logging.getLogger("podshelf.downloader")

progress: dict[int, dict] = {}  # episode_id -> {"done": bytes, "total": bytes, "speed": B/s}
_stop = threading.Event()
_cancel: set[int] = set()

AUDIO_EXT = {".mp3", ".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".opus", ".oga", ".flac", ".wav"}
TYPE_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
            "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/flac": ".flac",
            "video/mp4": ".mp4", "audio/wav": ".wav", "audio/x-wav": ".wav"}


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
    """Chronological position of the episode among all known episodes of the feed."""
    row = db.one(
        "SELECT COUNT(*) AS n FROM episodes WHERE podcast_id=? AND "
        "(COALESCE(pub_date,'') < COALESCE(?, '') OR (COALESCE(pub_date,'') = COALESCE(?, '') AND id < ?))",
        (ep["podcast_id"], ep["pub_date"], ep["pub_date"], ep["id"]),
    )
    return row["n"] + 1


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
            album=p["title"],
            title=ep["title"],
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


# ---------------------------------------------------------------- queue
def _claim():
    now = feeds.now_iso()
    with db.tx() as c:
        row = c.execute(
            "SELECT e.id FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
            "WHERE e.status='queued' AND (e.next_try IS NULL OR e.next_try<=?) "
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
    base = naming.episode_basename(db.get_setting("episode_format"), p["title"], ep["title"],
                                   ep["pub_date"], episode_number(ep), p["author"] or "")
    tmp = os.path.join(folder, f".{base}.{eid}.part")
    progress[eid] = {"done": 0, "total": 0, "speed": 0}
    try:
        with client.stream("GET", ep["enclosure_url"]) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            progress[eid]["total"] = total
            ext = _guess_ext(str(r.url), r.headers.get("content-type") or ep["enclosure_type"])
            done, t0 = 0, time.monotonic()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(256 * 1024):
                    if eid in _cancel:
                        raise InterruptedError("cancelled")
                    fh.write(chunk)
                    done += len(chunk)
                    el = max(0.001, time.monotonic() - t0)
                    progress[eid].update(done=done, speed=int(done / el))
        if total and done < total:
            raise IOError(f"incomplete download ({done} of {total} bytes)")
        if done < 10_000:
            raise IOError(f"file too small ({done} bytes), probably not audio")
        ext = _sniff_ext(tmp, ext)
        final = os.path.join(folder, base + ext)
        if os.path.exists(final) and not _same_episode(final, ep):
            final = os.path.join(folder, f"{base} ({eid}){ext}")
        os.replace(tmp, final)
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
                  "error=NULL, next_try=NULL WHERE id=?",
                  (final, os.path.getsize(final), feeds.now_iso(), eid))
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    try:
        tag_episode(ep, p, client)
    except Exception as e:  # keep the file even if tagging fails
        log.warning("tagging %s failed: %s", final, e)
        with db.tx() as c:
            c.execute("UPDATE episodes SET error=? WHERE id=?", (f"tagging: {e}"[:500], eid))
    log.info("downloaded %s / %s", p["title"], ep["title"])
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
    p = db.one("SELECT keep_count FROM podcasts WHERE id=?", (pid,))
    if not p or p["keep_count"] <= 0:
        return
    rows = db.query(
        "SELECT id, file_path FROM episodes WHERE podcast_id=? AND status='done' AND pinned=0 "
        "ORDER BY pub_date DESC, id DESC", (pid,))
    for r in rows[p["keep_count"]:]:
        delete_file(r["id"], status="deleted")


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
                  "pinned=? WHERE id=? AND status NOT IN ('done','downloading')",
                  (1 if pinned else 0, eid))
    feeds.wake_downloads.set()


def retag_podcast(pid: int):
    p = db.one("SELECT * FROM podcasts WHERE id=?", (pid,))
    feeds.save_cover(p)
    n = 0
    with feeds.http_client(30) as client:
        for ep in db.query("SELECT * FROM episodes WHERE podcast_id=? AND status='done'", (pid,)):
            if ep["file_path"] and os.path.isfile(ep["file_path"]):
                try:
                    tag_episode(ep, p, client)
                    n += 1
                except Exception as e:
                    log.warning("retag %s failed: %s", ep["file_path"], e)
    return n


def adopt(ids):
    """Take over files that were already on disk: rename to our scheme and tag."""
    pid = None
    with feeds.http_client(30) as client:
        for eid in ids:
            ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
            p = db.one("SELECT * FROM podcasts WHERE id=?", (ep["podcast_id"],))
            pid = p["id"]
            base = naming.episode_basename(db.get_setting("episode_format"), p["title"], ep["title"],
                                           ep["pub_date"], episode_number(ep), p["author"] or "")
            ext = os.path.splitext(ep["file_path"])[1].lower()
            target = os.path.join(feeds.podcast_dir(p), base + ext)
            path = ep["file_path"]
            if target != path and not os.path.exists(target):
                try:
                    os.replace(path, target)
                    path = target
                except OSError as e:
                    log.warning("rename %s failed: %s", path, e)
            with db.tx() as c:
                c.execute("UPDATE episodes SET file_path=? WHERE id=?", (path, eid))
            try:
                tag_episode(db.one("SELECT * FROM episodes WHERE id=?", (eid,)), p, client)
            except Exception as e:
                log.warning("tagging %s failed: %s", path, e)
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
