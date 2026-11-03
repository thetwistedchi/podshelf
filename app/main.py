import hmac
import logging
import math
import os
import secrets
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import abs_import, config, db, downloader, feeds, naming, postprocess, scheduler, sync

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("podshelf")

BASE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


def _secret_key() -> str:
    if config.SECRET_KEY:
        return config.SECRET_KEY
    path = os.path.join(config.DATA_DIR, ".secret_key")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    if os.path.isfile(path):
        return open(path).read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as fh:
        fh.write(key)
    return key


@asynccontextmanager
async def lifespan(_app):
    os.makedirs(config.PODCAST_DIR, exist_ok=True)
    db.init()
    downloader.start()
    scheduler.start()
    sync.start()
    log.info("Podshelf started. Podcasts: %s, data: %s, login %s", config.PODCAST_DIR,
             config.DATA_DIR, "enabled" if config.ADMIN_PASSWORD else "disabled")
    yield


app = FastAPI(title="Podshelf", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")

OPEN_PATHS = ("/login", "/static", "/health")


@app.middleware("http")
async def auth(request: Request, call_next):
    if config.ADMIN_PASSWORD and not request.url.path.startswith(OPEN_PATHS):
        if not request.session.get("user"):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "login required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# added after the auth middleware so it runs first and the session is available
app.add_middleware(SessionMiddleware, secret_key=_secret_key(), max_age=60 * 60 * 24 * 30,
                   same_site="lax", session_cookie="podshelf")


# ---------------------------------------------------------------- helpers
def flash(request: Request, msg: str, kind="ok"):
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, **ctx):
    ctx.setdefault("flashes", request.session.pop("flash", []))
    ctx["login_enabled"] = bool(config.ADMIN_PASSWORD)
    ctx["counts"] = queue_counts()
    return templates.TemplateResponse(request, name, ctx)


def back(url: str):
    return RedirectResponse(url, status_code=303)


def queue_counts():
    rows = db.query("SELECT status, COUNT(*) n FROM episodes WHERE status IN "
                    "('queued','downloading','failed') GROUP BY status")
    out = {"queued": 0, "downloading": 0, "failed": 0}
    out.update({r["status"]: r["n"] for r in rows})
    return out


def fmt_size(n):
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_date(iso, with_time=False):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
        return d.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")
    except ValueError:
        return iso


def fmt_duration(v):
    if not v:
        return ""
    try:
        if ":" in v:
            parts = [int(float(x)) for x in v.split(":")]
            while len(parts) < 3:
                parts.insert(0, 0)
            h, m, s = parts
        else:
            secs = int(float(v))
            h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
        return f"{h}h {m:02d}m" if h else f"{m}m"
    except ValueError:
        return v


def fmt_secs(n):
    if not n:
        return ""
    n = int(n)
    h, m, s = n // 3600, n % 3600 // 60, n % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_hours(n):
    n = int(n or 0)
    if n < 60:
        return f"{n} s"
    if n < 3600:
        return f"{n // 60} min"
    return f"{n // 3600} h {n % 3600 // 60:02d} m"


templates.env.filters.update(size=fmt_size, date=fmt_date, duration=fmt_duration, secs=fmt_secs,
                             hours=fmt_hours)
templates.env.globals.update(ptitle=feeds.ptitle, ep_title=downloader.ep_title)


# ---------------------------------------------------------------- auth
@app.get("/login")
def login_page(request: Request):
    if not config.ADMIN_PASSWORD:
        return back("/")
    return templates.TemplateResponse(request, "login.html",
                                      {"error": request.session.pop("login_error", None)})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ok = hmac.compare_digest(username, config.ADMIN_USER) and \
        hmac.compare_digest(password, config.ADMIN_PASSWORD)
    if not ok:
        request.session["login_error"] = "Wrong username or password"
        return back("/login")
    request.session["user"] = username
    return back("/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login" if config.ADMIN_PASSWORD else "/")


@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- podcasts
def tags_by_podcast():
    out = {}
    for r in db.query("SELECT podcast_id, tag FROM podcast_tags ORDER BY tag COLLATE NOCASE"):
        out.setdefault(r["podcast_id"], []).append(r["tag"])
    return out


@app.get("/")
def index(request: Request, tag: str = ""):
    rows = db.query("""
        SELECT p.*,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id AND e.status='done') AS done,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id) AS total,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id AND e.status IN ('queued','downloading')) AS pending,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id AND e.status='failed') AS failed,
          (SELECT MAX(pub_date) FROM episodes e WHERE e.podcast_id=p.id) AS latest,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id AND e.status='done' AND e.played=0) AS unplayed
        FROM podcasts p ORDER BY COALESCE(p.title_override, p.title) COLLATE NOCASE""")
    tags = tags_by_podcast()
    all_tags = sorted({t for ts in tags.values() for t in ts}, key=str.lower)
    if tag:
        rows = [r for r in rows if tag in tags.get(r["id"], [])]
    return render(request, "index.html", podcasts=rows, tags=tags, all_tags=all_tags, tag=tag,
                  refreshing={r["id"] for r in rows if feeds.is_refreshing(r["id"])},
                  defaults={k: db.get_setting(k) for k in config.DEFAULT_SETTINGS})


@app.post("/podcasts/add")
def add(request: Request, feed_url: str = Form(...), keep: int = Form(None), initial: int = Form(None),
        check_hours: int = Form(None), max_new: int = Form(None), auth_user: str = Form(""),
        auth_pass: str = Form("")):
    url = feed_url.strip()
    if not url.lower().startswith(("http://", "https://")):
        flash(request, "Feed URL must start with http:// or https://", "error")
        return back("/")
    auth_user = auth_user.strip()
    auth = feeds.httpx.BasicAuth(auth_user, auth_pass) if auth_user else None
    try:
        meta, eps = feeds.fetch_feed(url, auth)
    except Exception as e:
        hint = " This feed needs a login, enter it under “Premium feed”." if "401" in str(e) else ""
        flash(request, f"Could not read feed: {e}{hint}", "error")
        return back("/")
    pid = feeds.add_podcast(url, keep, initial, check_hours, max_new)
    if auth_user:
        with db.tx() as c:
            c.execute("UPDATE podcasts SET auth_user=?, auth_pass=? WHERE id=?", (auth_user, auth_pass, pid))
    feeds.refresh_async(pid)
    flash(request, f"Added “{meta['title']}” ({len(eps)} episodes in feed)")
    return back(f"/podcasts/{pid}")


@app.get("/api/search")
def search(q: str):
    try:
        res = feeds.itunes_search(q)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    known = {r["feed_url"] for r in db.query("SELECT feed_url FROM podcasts")}
    for r in res:
        r["subscribed"] = r["feed_url"] in known
    return res


PAGE = 50


@app.get("/podcasts/{pid}")
def podcast(request: Request, pid: int, status: str = "all", q: str = "", page: int = 1,
            order: str = "desc"):
    p = db.one("SELECT * FROM podcasts WHERE id=?", (pid,))
    if not p:
        flash(request, "Podcast not found", "error")
        return back("/")
    where, params = ["podcast_id=?"], [pid]
    if status == "downloaded":
        where.append("status='done'")
    elif status == "pending":
        where.append("status IN ('queued','downloading')")
    elif status == "failed":
        where.append("status='failed'")
    elif status == "available":
        where.append("status IN ('new','skipped','deleted')")
    elif status == "unplayed":
        where.append("status='done' AND played=0")
    elif status == "played":
        where.append("played=1")
    if q:
        where.append("(title LIKE ? OR title_override LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    w = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM episodes WHERE {w}", params)["n"]
    pages = max(1, math.ceil(total / PAGE))
    page = min(max(1, page), pages)
    direction = "ASC" if order == "asc" else "DESC"
    eps = db.query(f"SELECT * FROM episodes WHERE {w} ORDER BY pub_date {direction}, id {direction} "
                   f"LIMIT ? OFFSET ?", params + [PAGE, (page - 1) * PAGE])
    # chronological numbers for display
    nums = {r["id"]: r["number_override"] or i for i, r in enumerate(db.query(
        "SELECT id, number_override FROM episodes WHERE podcast_id=? ORDER BY COALESCE(pub_date,''), id",
        (pid,)), 1)}
    stats = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) n FROM episodes WHERE podcast_id=? GROUP BY status", (pid,))}
    disk = db.one("SELECT COALESCE(SUM(file_size),0) s FROM episodes WHERE podcast_id=? AND status='done'",
                  (pid,))["s"]
    unplayed = db.one("SELECT COUNT(*) n FROM episodes WHERE podcast_id=? AND status='done' AND played=0",
                      (pid,))["n"]
    return render(request, "podcast.html", p=p, episodes=eps, nums=nums, stats=stats, disk=disk,
                  unplayed=unplayed, tags=tags_by_podcast().get(pid, []), ffmpeg=bool(postprocess.FFMPEG),
                  ad_keywords=db.get_setting("ad_keywords"),
                  status=status, q=q, page=page, pages=pages, total=total, order=order,
                  refreshing=feeds.is_refreshing(pid))


def _int(v, default=0, lo=0):
    try:
        return max(lo, int(str(v).strip()))
    except (TypeError, ValueError):
        return default


@app.post("/podcasts/{pid}/settings")
async def podcast_settings(request: Request, pid: int):
    f = await request.form()
    p = db.one("SELECT * FROM podcasts WHERE id=?", (pid,))
    if not p:
        return back("/")
    folder = str(f.get("folder", "")).strip()
    new_folder = naming.safe(folder) if folder else p["folder"]
    if new_folder != p["folder"] and p["folder"]:
        if db.one("SELECT 1 FROM podcasts WHERE folder=? AND id<>?", (new_folder, pid)):
            flash(request, "Another podcast already uses that folder", "error")
            return back(f"/podcasts/{pid}")
        old = os.path.join(config.PODCAST_DIR, p["folder"])
        new = os.path.join(config.PODCAST_DIR, new_folder)
        if os.path.exists(new):
            flash(request, "Target folder already exists", "error")
            return back(f"/podcasts/{pid}")
        if os.path.isdir(old):
            os.rename(old, new)
        with db.tx() as c:
            c.execute("UPDATE episodes SET file_path=? || SUBSTR(file_path, ?) WHERE podcast_id=? "
                      "AND file_path LIKE ?", (new, len(old) + 1, pid, old + os.sep + "%"))
    split = str(f.get("split_mode", "none"))
    split = split if split in ("none", "season", "bonus", "both") else "none"
    title_override = str(f.get("title_override", "")).strip() or None
    auth_user = str(f.get("auth_user", "")).strip()
    auth_pass = str(f.get("auth_pass", ""))
    if auth_user and not auth_pass:
        auth_pass = p["auth_pass"]  # empty field = keep the saved password
    delete_played = str(f.get("delete_played_days", "")).strip()
    with db.tx() as c:
        c.execute(
            "UPDATE podcasts SET keep_count=?, keep_days=?, check_hours=?, max_new=?, folder=?, auto_download=?, "
            "active=?, feed_url=?, title_override=?, include_filter=?, exclude_filter=?, min_minutes=?, "
            "max_minutes=?, trim_start=?, trim_end=?, cut_ad_chapters=?, auth_user=?, auth_pass=?, "
            "split_mode=?, delete_played_days=? WHERE id=?",
            (_int(f.get("keep_count"), p["keep_count"]), _int(f.get("keep_days"), p["keep_days"]),
             _int(f.get("check_hours"), p["check_hours"], 1), _int(f.get("max_new"), p["max_new"]),
             new_folder, 1 if f.get("auto_download") else 0, 1 if f.get("active") else 0,
             str(f.get("feed_url", "")).strip() or p["feed_url"], title_override,
             str(f.get("include_filter", "")).strip(), str(f.get("exclude_filter", "")).strip(),
             _int(f.get("min_minutes")), _int(f.get("max_minutes")),
             _int(f.get("trim_start")), _int(f.get("trim_end")), 1 if f.get("cut_ad_chapters") else 0,
             auth_user, auth_pass if auth_user else "", split,
             _int(delete_played, -1, -1) if delete_played != "" else -1, pid))
        c.execute("DELETE FROM podcast_tags WHERE podcast_id=?", (pid,))
        for t in {t.strip() for t in str(f.get("tags", "")).split(",") if t.strip()}:
            c.execute("INSERT INTO podcast_tags(podcast_id, tag) VALUES (?, ?)", (pid, t[:40]))
    downloader.apply_retention(pid)
    if title_override != p["title_override"] or split != p["split_mode"]:
        threading.Thread(target=downloader.retag_podcast, args=(pid,), daemon=True).start()
        flash(request, "Settings saved. Files are being renamed and retagged in the background.")
    else:
        flash(request, "Settings saved")
    return back(f"/podcasts/{pid}")


@app.post("/podcasts/{pid}/played")
def mark_all_played(request: Request, pid: int, played: int = Form(1)):
    with db.tx() as c:
        if played:
            n = c.execute("UPDATE episodes SET played=1, played_at=? WHERE podcast_id=? AND played=0 "
                          "AND status='done'", (feeds.now_iso(), pid)).rowcount
        else:
            n = c.execute("UPDATE episodes SET played=0, played_at=NULL WHERE podcast_id=? AND played=1",
                          (pid,)).rowcount
    flash(request, f"Marked {n} episodes as {'played' if played else 'unplayed'}")
    if played:
        threading.Thread(target=sync.run, daemon=True).start()
    return back(f"/podcasts/{pid}")


@app.post("/podcasts/{pid}/refresh")
def refresh(request: Request, pid: int):
    feeds.refresh_async(pid)
    flash(request, "Checking feed for new episodes…")
    return back(request.headers.get("referer") or f"/podcasts/{pid}")


@app.post("/podcasts/refresh-all")
def refresh_all(request: Request):
    for r in db.query("SELECT id FROM podcasts WHERE active=1"):
        feeds.refresh_async(r["id"])
    flash(request, "Checking all feeds…")
    return back("/")


@app.post("/podcasts/{pid}/latest")
def download_latest(request: Request, pid: int, n: int = Form(10)):
    rows = db.query("SELECT id FROM episodes WHERE podcast_id=? ORDER BY pub_date DESC LIMIT ?", (pid, n))
    for r in rows:
        downloader.queue(r["id"], pinned=False)
    flash(request, f"Queued the latest {n} episodes")
    return back(f"/podcasts/{pid}")


@app.post("/podcasts/{pid}/all")
def download_all(request: Request, pid: int):
    # downloading everything only makes sense if retention keeps everything
    with db.tx() as c:
        c.execute("UPDATE podcasts SET keep_count=0 WHERE id=?", (pid,))
        n = c.execute("UPDATE episodes SET status='queued', attempts=0, next_try=NULL, error=NULL "
                      "WHERE podcast_id=? AND status IN ('new','skipped','deleted','failed')",
                      (pid,)).rowcount
    feeds.wake_downloads.set()
    flash(request, f"Queued {n} episodes. This podcast now keeps all episodes.")
    return back(f"/podcasts/{pid}")


@app.post("/podcasts/{pid}/retag")
def retag(request: Request, pid: int):
    threading.Thread(target=downloader.retag_podcast, args=(pid,), daemon=True).start()
    flash(request, "Renaming files and rewriting tags and covers in the background")
    return back(f"/podcasts/{pid}")


@app.post("/podcasts/{pid}/delete")
def delete_podcast(request: Request, pid: int, delete_files: str = Form(None)):
    p = db.one("SELECT * FROM podcasts WHERE id=?", (pid,))
    if not p:
        return back("/")
    if delete_files:
        for ep in db.query("SELECT id FROM episodes WHERE podcast_id=? AND file_path IS NOT NULL", (pid,)):
            downloader.delete_file(ep["id"])
        cover = os.path.join(config.PODCAST_DIR, p["folder"] or "", "cover.jpg")
        if p["folder"] and os.path.isfile(cover):
            os.remove(cover)
        try:
            os.rmdir(os.path.join(config.PODCAST_DIR, p["folder"]))
        except (OSError, TypeError):
            pass
    with db.tx() as c:
        c.execute("DELETE FROM episodes WHERE podcast_id=?", (pid,))
        c.execute("DELETE FROM podcasts WHERE id=?", (pid,))
    flash(request, f"Removed “{feeds.ptitle(p)}”")
    return back("/")


@app.get("/podcasts/{pid}/cover")
def cover(pid: int):
    p = db.one("SELECT * FROM podcasts WHERE id=?", (pid,))
    if p and p["folder"]:
        path = os.path.join(config.PODCAST_DIR, p["folder"], "cover.jpg")
        if os.path.isfile(path):
            return FileResponse(path, headers={"Cache-Control": "max-age=3600"})
    if p and p["image_url"]:
        path = feeds.cached_image(p["image_url"])
        if path:
            return FileResponse(path, headers={"Cache-Control": "max-age=3600"})
    return FileResponse(os.path.join(BASE, "static", "placeholder.svg"))


# ---------------------------------------------------------------- episodes
@app.post("/api/episodes/{eid}/edit")
async def episode_edit(eid: int, request: Request):
    data = await request.json()
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    if not ep:
        return JSONResponse({"error": "not found"}, status_code=404)
    title = (data.get("title") or "").strip()
    number = data.get("number")
    try:
        number = int(number) if str(number).strip() else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "number must be a whole number"}, status_code=400)
    with db.tx() as c:
        c.execute("UPDATE episodes SET title_override=?, number_override=? WHERE id=?",
                  (title if title and title != ep["title"] else None, number, eid))
    if ep["status"] == "done":
        try:
            downloader.retag_episode(eid)
        except Exception as e:
            return JSONResponse({"error": f"saved, but retagging failed: {e}"}, status_code=500)
    return episode_json(eid)


@app.post("/api/episodes/{eid}/{action}")
def episode_action(eid: int, action: str):
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    if not ep:
        return JSONResponse({"error": "not found"}, status_code=404)
    if action == "download":
        downloader.queue(eid, pinned=True)
    elif action == "delete":
        downloader.delete_file(eid, status="deleted")
    elif action == "skip":
        if ep["status"] in ("queued", "failed", "new"):
            with db.tx() as c:
                c.execute("UPDATE episodes SET status='skipped' WHERE id=?", (eid,))
    elif action == "cancel":
        downloader.cancel(eid)
        with db.tx() as c:
            c.execute("UPDATE episodes SET status='new' WHERE id=? AND status='queued'", (eid,))
    elif action == "played":
        new = 0 if ep["played"] else 1
        with db.tx() as c:
            c.execute("UPDATE episodes SET played=?, played_at=? WHERE id=?",
                      (new, feeds.now_iso() if new else None, eid))
        sync.push(eid, bool(new))
        if new:
            downloader.apply_retention(ep["podcast_id"])
    elif action == "pin":
        with db.tx() as c:
            c.execute("UPDATE episodes SET pinned=1-pinned WHERE id=?", (eid,))
        downloader.apply_retention(ep["podcast_id"])
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    return episode_json(eid)


def episode_json(eid):
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    out = dict(ep)
    out.pop("description", None)
    out["progress"] = downloader.progress.get(eid)
    return out


@app.get("/episodes/{eid}/audio")
def audio(eid: int):
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    if ep and ep["file_path"] and os.path.isfile(ep["file_path"]):
        return FileResponse(ep["file_path"])
    if ep and ep["enclosure_url"]:
        return RedirectResponse(ep["enclosure_url"])
    return Response(status_code=404)


@app.get("/api/episodes/{eid}")
def episode_detail(eid: int):
    ep = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    if not ep:
        return JSONResponse({"error": "not found"}, status_code=404)
    out = dict(ep)
    out["chapters"] = postprocess.loads(ep["file_chapters"] if ep["status"] == "done" and ep["file_chapters"]
                                        else ep["chapters"])
    out.pop("auth_pass", None)
    return out


# ---------------------------------------------------------------- queue
@app.get("/queue")
def queue_page(request: Request):
    active = db.query("SELECT e.*, COALESCE(p.title_override, p.title) AS ptitle FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
                      "WHERE e.status='downloading'")
    queued = db.query("SELECT e.*, COALESCE(p.title_override, p.title) AS ptitle FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
                      "WHERE e.status='queued' ORDER BY e.pinned DESC, e.pub_date DESC LIMIT 300")
    failed = db.query("SELECT e.*, COALESCE(p.title_override, p.title) AS ptitle FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
                      "WHERE e.status='failed' ORDER BY e.pub_date DESC LIMIT 300")
    recent = db.query("SELECT e.*, COALESCE(p.title_override, p.title) AS ptitle FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
                      "WHERE e.status='done' ORDER BY e.downloaded_at DESC LIMIT 30")
    return render(request, "queue.html", active=active, queued=queued, failed=failed, recent=recent)


@app.post("/queue/retry-failed")
def retry_failed(request: Request):
    with db.tx() as c:
        n = c.execute("UPDATE episodes SET status='queued', attempts=0, next_try=NULL, error=NULL "
                      "WHERE status='failed'").rowcount
    feeds.wake_downloads.set()
    flash(request, f"Retrying {n} failed episodes")
    return back("/queue")


@app.post("/queue/clear-failed")
def clear_failed(request: Request):
    with db.tx() as c:
        n = c.execute("UPDATE episodes SET status='skipped' WHERE status='failed'").rowcount
    flash(request, f"Dismissed {n} failed episodes")
    return back("/queue")


@app.post("/queue/clear")
def clear_queue(request: Request):
    with db.tx() as c:
        n = c.execute("UPDATE episodes SET status='new' WHERE status='queued'").rowcount
    flash(request, f"Removed {n} episodes from the queue")
    return back("/queue")


@app.get("/api/status")
def status():
    active = []
    for r in db.query("SELECT e.id, e.title, COALESCE(p.title_override, p.title) AS ptitle FROM episodes e JOIN podcasts p "
                      "ON p.id=e.podcast_id WHERE e.status='downloading'"):
        pr = downloader.progress.get(r["id"]) or {}
        active.append({"id": r["id"], "title": r["title"], "podcast": r["ptitle"],
                       "done": pr.get("done", 0), "total": pr.get("total", 0), "speed": pr.get("speed", 0)})
    refreshing = [r["id"] for r in db.query("SELECT id FROM podcasts") if feeds.is_refreshing(r["id"])]
    return {"counts": queue_counts(), "active": active, "refreshing": refreshing}


# ---------------------------------------------------------------- settings / import
@app.get("/settings")
def settings_page(request: Request):
    s_ = {k: db.get_setting(k) for k in config.DEFAULT_SETTINGS}
    secrets_set = {k: bool(s_[k]) for k in config.SECRET_SETTINGS}
    for k in config.SECRET_SETTINGS:
        s_[k] = ""
    return render(request, "settings.html", s=s_, secrets_set=secrets_set,
                  sample=naming.sample(s_["podcast_format"], s_["episode_format"]),
                  podcast_dir=config.PODCAST_DIR, sync_status=sync.status, ffmpeg=bool(postprocess.FFMPEG))


@app.post("/settings")
async def save_settings(request: Request):
    _apply_settings(await request.form())
    feeds.wake_downloads.set()
    flash(request, "Settings saved")
    return back("/settings")


def _apply_settings(form):
    full_form = form.get("_form") == "settings"  # checkboxes are only absent-means-off on the full form
    for k in config.DEFAULT_SETTINGS:
        if k in config.BOOL_SETTINGS:
            if not full_form:
                continue
            db.set_setting(k, "1" if form.get(k) else "0")
            continue
        if k not in form:
            continue
        v = str(form[k]).strip()
        if k in config.SECRET_SETTINGS:
            if form.get(f"clear_{k}"):
                db.set_setting(k, "")
            elif v:
                db.set_setting(k, v)  # empty = keep the saved secret
            continue
        if k in config.INT_SETTINGS:
            try:
                v = str(max(0, int(v)))
            except ValueError:
                continue
        if k in ("window_start", "window_end") and v:
            try:
                v = str(min(23, max(0, int(v))))
            except ValueError:
                v = ""
        if k.endswith("_format") and not v:
            v = config.DEFAULT_SETTINGS[k]
        db.set_setting(k, v)


@app.post("/sync/run")
async def sync_now(request: Request):
    form = await request.form()
    if form:  # "Sync now" inside the settings form saves the form first
        _apply_settings(form)
    st = await run_in_threadpool(sync.run)
    parts = []
    for name in ("plex", "emby"):
        r = st.get(name)
        if r is None:
            continue
        if r.get("ok"):
            parts.append(f"{name.title()}: {r['matched']} matched, {r['pulled']} marked played here, "
                         f"{r['pushed']} sent")
        else:
            parts.append(f"{name.title()} failed: {r.get('error')}")
    ok = all((st.get(n) or {"ok": True}).get("ok") for n in ("plex", "emby"))
    flash(request, " · ".join(parts) or "Nothing to sync. Add Plex or Emby details first.",
          "ok" if ok else "error")
    return back("/settings#sync")


# ---------------------------------------------------------------- backup
@app.get("/backup")
def backup():
    fd, path = tempfile.mkstemp(suffix=".db", dir=config.DATA_DIR)
    os.close(fd)
    db.backup_to(path)
    name = f"podshelf-backup-{datetime.now():%Y-%m-%d}.db"
    from starlette.background import BackgroundTask
    return FileResponse(path, filename=name, media_type="application/octet-stream",
                        background=BackgroundTask(os.remove, path))


@app.post("/restore")
async def restore(request: Request, file: UploadFile = File(...)):
    fd, path = tempfile.mkstemp(suffix=".db", dir=config.DATA_DIR)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(await file.read())
        db.restore_from(path)
    except Exception as e:
        flash(request, f"Restore failed: {e}", "error")
        return back("/settings#backup")
    finally:
        if os.path.exists(path):
            os.remove(path)
    downloader.verify_files()
    flash(request, "Backup restored")
    return back("/")


# ---------------------------------------------------------------- stats + duplicates
@app.get("/stats")
def stats_page(request: Request):
    tot = db.one("""SELECT COUNT(*) episodes,
        SUM(status='done') downloaded, SUM(status='failed') failed, SUM(played) played,
        COALESCE(SUM(CASE WHEN status='done' THEN file_size END),0) disk,
        COALESCE(SUM(CASE WHEN status='done' THEN length_secs END),0) secs,
        COALESCE(SUM(cut_secs),0) cut, SUM(skip_reason IS NOT NULL AND status='skipped') filtered
        FROM episodes""")
    per = db.query("""SELECT p.id, COALESCE(p.title_override, p.title) title,
        SUM(e.status='done') downloaded, COUNT(e.id) total, SUM(e.played) played,
        COALESCE(SUM(CASE WHEN e.status='done' THEN e.file_size END),0) disk,
        COALESCE(SUM(CASE WHEN e.status='done' THEN e.length_secs END),0) secs
        FROM podcasts p LEFT JOIN episodes e ON e.podcast_id=p.id GROUP BY p.id ORDER BY disk DESC""")
    since = (datetime.now(timezone.utc) - timedelta(weeks=12)).isoformat()
    weeks = {}
    for r in db.query("SELECT downloaded_at FROM episodes WHERE downloaded_at>=?", (since,)):
        d = datetime.fromisoformat(r["downloaded_at"])
        k = (d - timedelta(days=d.weekday())).strftime("%d.%m.")
        weeks[k] = weeks.get(k, 0) + 1
    week_list = []
    for i in range(11, -1, -1):
        d = datetime.now(timezone.utc) - timedelta(weeks=i)
        k = (d - timedelta(days=d.weekday())).strftime("%d.%m.")
        week_list.append((k, weeks.get(k, 0)))
    dupes = len(find_duplicates())
    return render(request, "stats.html", t=tot, per=per, weeks=week_list,
                  max_week=max([n for _, n in week_list] + [1]), podcasts=len(per), dupes=dupes)


def find_duplicates():
    """Groups of episodes in the same podcast with the same title or the same audio URL."""
    rows = db.query("SELECT e.*, COALESCE(p.title_override, p.title) ptitle FROM episodes e "
                    "JOIN podcasts p ON p.id=e.podcast_id ORDER BY e.pub_date DESC")
    groups = {}
    for r in rows:
        n = feeds._norm(r["title"])
        keys = [("u", r["podcast_id"], r["enclosure_url"])]
        if len(n) >= 8:
            keys.append(("t", r["podcast_id"], n))
        for k in keys:
            groups.setdefault(k, []).append(r)
    seen, out = set(), []
    for g in groups.values():
        ids = tuple(sorted(r["id"] for r in g))
        if len(g) > 1 and ids not in seen:
            seen.add(ids)
            out.append(g)
    return out


@app.get("/duplicates")
def duplicates_page(request: Request):
    return render(request, "duplicates.html", groups=find_duplicates())


@app.get("/api/naming-preview")
def naming_preview(podcast_format: str = "{podcast}", episode_format: str = "{date} - {title}"):
    return {"sample": naming.sample(podcast_format, episode_format)}


@app.post("/opml/import")
async def opml_import(request: Request, file: UploadFile = File(...)):
    try:
        urls = feeds.parse_opml(await file.read())
    except Exception as e:
        flash(request, f"Invalid OPML: {e}", "error")
        return back("/settings")
    added = 0
    for u in urls:
        if not db.one("SELECT 1 FROM podcasts WHERE feed_url=?", (u,)):
            feeds.refresh_async(feeds.add_podcast(u))
            added += 1
    flash(request, f"Imported {added} new podcasts ({len(urls) - added} already subscribed). "
                   "Episodes are loading in the background.")
    return back("/")


@app.get("/opml/export")
def opml_export():
    return PlainTextResponse(feeds.export_opml(), media_type="text/x-opml",
                             headers={"Content-Disposition": 'attachment; filename="podshelf.opml"'})


@app.post("/abs/import")
def abs_import_route(request: Request, abs_url: str = Form(...), abs_key: str = Form(...)):
    try:
        res = abs_import.import_from_abs(abs_url, abs_key, int(db.get_setting("default_check_hours")))
    except Exception as e:
        flash(request, f"Audiobookshelf import failed: {e}", "error")
        return back("/settings")
    msg = f"Imported {res['added']} podcasts from Audiobookshelf, {res['skipped']} skipped"
    if res["errors"]:
        msg += f", {len(res['errors'])} errors"
    flash(request, msg, "error" if res["errors"] and not res["added"] else "ok")
    return back("/")
