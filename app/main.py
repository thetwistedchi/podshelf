import hmac
import logging
import math
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import abs_import, config, db, downloader, feeds, naming, scheduler

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


templates.env.filters.update(size=fmt_size, date=fmt_date, duration=fmt_duration)


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
@app.get("/")
def index(request: Request):
    rows = db.query("""
        SELECT p.*,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id AND e.status='done') AS done,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id) AS total,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id AND e.status IN ('queued','downloading')) AS pending,
          (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id AND e.status='failed') AS failed,
          (SELECT MAX(pub_date) FROM episodes e WHERE e.podcast_id=p.id) AS latest
        FROM podcasts p ORDER BY p.title COLLATE NOCASE""")
    return render(request, "index.html", podcasts=rows,
                  refreshing={r["id"] for r in rows if feeds.is_refreshing(r["id"])},
                  defaults={k: db.get_setting(k) for k in config.DEFAULT_SETTINGS})


@app.post("/podcasts/add")
def add(request: Request, feed_url: str = Form(...), keep: int = Form(None), initial: int = Form(None),
        check_hours: int = Form(None), max_new: int = Form(None)):
    url = feed_url.strip()
    if not url.lower().startswith(("http://", "https://")):
        flash(request, "Feed URL must start with http:// or https://", "error")
        return back("/")
    try:
        meta, eps = feeds.fetch_feed(url)
    except Exception as e:
        flash(request, f"Could not read feed: {e}", "error")
        return back("/")
    pid = feeds.add_podcast(url, keep, initial, check_hours, max_new)
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
    if q:
        where.append("title LIKE ?")
        params.append(f"%{q}%")
    w = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM episodes WHERE {w}", params)["n"]
    pages = max(1, math.ceil(total / PAGE))
    page = min(max(1, page), pages)
    direction = "ASC" if order == "asc" else "DESC"
    eps = db.query(f"SELECT * FROM episodes WHERE {w} ORDER BY pub_date {direction}, id {direction} "
                   f"LIMIT ? OFFSET ?", params + [PAGE, (page - 1) * PAGE])
    # chronological numbers for display
    nums = {r["id"]: i for i, r in enumerate(db.query(
        "SELECT id FROM episodes WHERE podcast_id=? ORDER BY COALESCE(pub_date,''), id", (pid,)), 1)}
    stats = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) n FROM episodes WHERE podcast_id=? GROUP BY status", (pid,))}
    disk = db.one("SELECT COALESCE(SUM(file_size),0) s FROM episodes WHERE podcast_id=? AND status='done'",
                  (pid,))["s"]
    return render(request, "podcast.html", p=p, episodes=eps, nums=nums, stats=stats, disk=disk,
                  status=status, q=q, page=page, pages=pages, total=total, order=order,
                  refreshing=feeds.is_refreshing(pid))


@app.post("/podcasts/{pid}/settings")
def podcast_settings(request: Request, pid: int, title: str = Form(""), keep_count: int = Form(10),
                     check_hours: int = Form(6), max_new: int = Form(3), folder: str = Form(""),
                     auto_download: str = Form(None), active: str = Form(None), feed_url: str = Form("")):
    p = db.one("SELECT * FROM podcasts WHERE id=?", (pid,))
    new_folder = naming.safe(folder) if folder.strip() else p["folder"]
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
            c.execute("UPDATE episodes SET file_path=REPLACE(file_path, ?, ?) WHERE podcast_id=? "
                      "AND file_path IS NOT NULL", (old + os.sep, new + os.sep, pid))
    with db.tx() as c:
        c.execute("UPDATE podcasts SET keep_count=?, check_hours=?, max_new=?, folder=?, auto_download=?, "
                  "active=?, feed_url=? WHERE id=?",
                  (max(0, keep_count), max(1, check_hours), max(0, max_new), new_folder,
                   1 if auto_download else 0, 1 if active else 0, feed_url.strip() or p["feed_url"], pid))
    downloader.apply_retention(pid)
    flash(request, "Settings saved")
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


@app.post("/podcasts/{pid}/retag")
def retag(request: Request, pid: int):
    threading.Thread(target=downloader.retag_podcast, args=(pid,), daemon=True).start()
    flash(request, "Rewriting tags and covers in the background")
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
    flash(request, f"Removed “{p['title']}”")
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
    return dict(ep)


# ---------------------------------------------------------------- queue
@app.get("/queue")
def queue_page(request: Request):
    active = db.query("SELECT e.*, p.title AS ptitle FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
                      "WHERE e.status='downloading'")
    queued = db.query("SELECT e.*, p.title AS ptitle FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
                      "WHERE e.status='queued' ORDER BY e.pinned DESC, e.pub_date DESC LIMIT 300")
    failed = db.query("SELECT e.*, p.title AS ptitle FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
                      "WHERE e.status='failed' ORDER BY e.pub_date DESC LIMIT 300")
    recent = db.query("SELECT e.*, p.title AS ptitle FROM episodes e JOIN podcasts p ON p.id=e.podcast_id "
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
    for r in db.query("SELECT e.id, e.title, p.title AS ptitle FROM episodes e JOIN podcasts p "
                      "ON p.id=e.podcast_id WHERE e.status='downloading'"):
        pr = downloader.progress.get(r["id"]) or {}
        active.append({"id": r["id"], "title": r["title"], "podcast": r["ptitle"],
                       "done": pr.get("done", 0), "total": pr.get("total", 0), "speed": pr.get("speed", 0)})
    refreshing = [r["id"] for r in db.query("SELECT id FROM podcasts") if feeds.is_refreshing(r["id"])]
    return {"counts": queue_counts(), "active": active, "refreshing": refreshing}


# ---------------------------------------------------------------- settings / import
@app.get("/settings")
def settings_page(request: Request):
    s = {k: db.get_setting(k) for k in config.DEFAULT_SETTINGS}
    return render(request, "settings.html", s=s, sample=naming.sample(s["podcast_format"], s["episode_format"]),
                  podcast_dir=config.PODCAST_DIR)


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    for k in config.DEFAULT_SETTINGS:
        if k in ("strip_date_tag", "force_podcast_cover"):
            db.set_setting(k, "1" if form.get(k) else "0")
        elif k in form:
            v = str(form[k]).strip()
            if k.startswith("default_") or k == "parallel_downloads":
                try:
                    v = str(max(0, int(v)))
                except ValueError:
                    continue
            if v or k.endswith("_format"):
                db.set_setting(k, v or config.DEFAULT_SETTINGS[k])
    feeds.wake_downloads.set()
    flash(request, "Settings saved")
    return back("/settings")


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
