"""Played-state sync with Plex and Emby.

Episodes are matched by their last two path parts ("Podcast folder/file name"),
so it works no matter where Plex/Emby mount the podcast share. Played is sticky:
played anywhere counts as played everywhere. Un-marking in Podshelf is pushed
right away."""
import logging
import os
import threading
import time
from datetime import datetime, timezone

import httpx

from . import config, db, downloader

log = logging.getLogger("podshelf.sync")
status = {"last_run": None, "plex": None, "emby": None}
_lock = threading.Lock()


def _key(path: str) -> str:
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return "/".join(parts[-2:]).lower()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _client():
    return httpx.Client(timeout=30, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})


def _local_episodes():
    rows = db.query("SELECT id, file_path, played FROM episodes WHERE status='done' AND file_path IS NOT NULL")
    return {_key(r["file_path"]): r for r in rows}


def _mark_local(eid, played_at=None):
    with db.tx() as c:
        c.execute("UPDATE episodes SET played=1, played_at=COALESCE(?, ?) WHERE id=? AND played=0",
                  (played_at, _now(), eid))


# ---------------------------------------------------------------- Plex
def _plex_cfg():
    url, token = db.get_setting("plex_url"), db.get_setting("plex_token")
    return (url.rstrip("/"), token) if url and token else (None, None)


def _plex_tracks(c, url, token):
    params = {"X-Plex-Token": token}
    secs = c.get(f"{url}/library/sections", params=params).raise_for_status().json()
    wanted = (db.get_setting("plex_library") or "").strip().lower()
    out = []
    for s in secs["MediaContainer"].get("Directory", []):
        if s.get("type") != "artist" or (wanted and s.get("title", "").lower() != wanted):
            continue
        res = c.get(f"{url}/library/sections/{s['key']}/all", params={**params, "type": 10}) \
               .raise_for_status().json()
        for t in res["MediaContainer"].get("Metadata", []):
            for m in t.get("Media", []):
                for part in m.get("Part", []):
                    if part.get("file"):
                        out.append((part["file"], t))
    return out


def sync_plex(local):
    url, token = _plex_cfg()
    if not url:
        return None
    pulled = pushed = matched = 0
    with _client() as c:
        for path, t in _plex_tracks(c, url, token):
            ep = local.get(_key(path))
            if not ep:
                continue
            matched += 1
            plex_played = (t.get("viewCount") or 0) > 0
            if plex_played and not ep["played"]:
                ts = t.get("lastViewedAt")
                _mark_local(ep["id"], datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
                            if ts else None)
                pulled += 1
            elif ep["played"] and not plex_played:
                _plex_set(c, url, token, t["ratingKey"], True)
                pushed += 1
    return {"matched": matched, "pulled": pulled, "pushed": pushed}


def _plex_set(c, url, token, rating_key, played):
    c.get(f"{url}/:/{'scrobble' if played else 'unscrobble'}",
          params={"X-Plex-Token": token, "key": rating_key,
                  "identifier": "com.plexapp.plugins.library"}).raise_for_status()


# ---------------------------------------------------------------- Emby
def _emby_cfg():
    url, key, user = db.get_setting("emby_url"), db.get_setting("emby_key"), db.get_setting("emby_user")
    return (url.rstrip("/"), key, user) if url and key and user else (None, None, None)


def _emby_user_id(c, url, key, user):
    users = c.get(f"{url}/Users", params={"api_key": key}).raise_for_status().json()
    for u in users:
        if u.get("Name", "").lower() == user.lower():
            return u["Id"]
    raise ValueError(f"Emby user “{user}” not found")


def _emby_items(c, url, key, uid):
    params = {"api_key": key, "Recursive": "true", "IncludeItemTypes": "Audio", "Fields": "Path"}
    lib = (db.get_setting("emby_library") or "").strip().lower()
    if lib:
        for f in c.get(f"{url}/Library/VirtualFolders", params={"api_key": key}).raise_for_status().json():
            if f.get("Name", "").lower() == lib:
                params["ParentId"] = f.get("ItemId")
    return c.get(f"{url}/Users/{uid}/Items", params=params).raise_for_status().json().get("Items", [])


def sync_emby(local):
    url, key, user = _emby_cfg()
    if not url:
        return None
    pulled = pushed = matched = 0
    with _client() as c:
        uid = _emby_user_id(c, url, key, user)
        for it in _emby_items(c, url, key, uid):
            ep = local.get(_key(it.get("Path") or ""))
            if not ep:
                continue
            matched += 1
            ud = it.get("UserData") or {}
            emby_played = bool(ud.get("Played"))
            if emby_played and not ep["played"]:
                _mark_local(ep["id"], ud.get("LastPlayedDate"))
                pulled += 1
            elif ep["played"] and not emby_played:
                _emby_set(c, url, key, uid, it["Id"], True)
                pushed += 1
    return {"matched": matched, "pulled": pulled, "pushed": pushed}


def _emby_set(c, url, key, uid, item_id, played):
    method = c.post if played else c.delete
    method(f"{url}/Users/{uid}/PlayedItems/{item_id}", params={"api_key": key}).raise_for_status()


# ---------------------------------------------------------------- run
def run():
    if not _lock.acquire(blocking=False):
        return status
    try:
        local = _local_episodes()
        for name, fn in (("plex", sync_plex), ("emby", sync_emby)):
            try:
                res = fn(local)
                status[name] = res and {**res, "ok": True}
                if res and res["pulled"]:
                    local = _local_episodes()  # so the next server sees the new state
            except Exception as e:
                log.warning("%s sync failed: %s", name, e)
                status[name] = {"ok": False, "error": str(e)[:300]}
        status["last_run"] = _now()
        downloader.apply_retention_all()  # "delete after played" rules
    finally:
        _lock.release()
    return status


def push(eid: int, played: bool):
    """Send a manual played/unplayed change to Plex and Emby right away (best effort)."""
    ep = db.one("SELECT file_path FROM episodes WHERE id=?", (eid,))
    if not ep or not ep["file_path"]:
        return
    k = _key(ep["file_path"])

    def work():
        try:
            url, token = _plex_cfg()
            if url:
                with _client() as c:
                    for path, t in _plex_tracks(c, url, token):
                        if _key(path) == k:
                            _plex_set(c, url, token, t["ratingKey"], played)
        except Exception as e:
            log.info("plex push failed: %s", e)
        try:
            url, key, user = _emby_cfg()
            if url:
                with _client() as c:
                    uid = _emby_user_id(c, url, key, user)
                    for it in _emby_items(c, url, key, uid):
                        if _key(it.get("Path") or "") == k:
                            _emby_set(c, url, key, uid, it["Id"], played)
        except Exception as e:
            log.info("emby push failed: %s", e)

    threading.Thread(target=work, daemon=True).start()


def _loop():
    time.sleep(30)
    while True:
        try:
            minutes = max(5, int(db.get_setting("sync_minutes") or 15))
        except ValueError:
            minutes = 15
        if _plex_cfg()[0] or _emby_cfg()[0]:
            run()
        time.sleep(minutes * 60)


def start():
    threading.Thread(target=_loop, daemon=True, name="sync").start()


def test_paths():
    """Debug helper: a few example keys from each side."""
    return {"local": list(_local_episodes())[:5]}
