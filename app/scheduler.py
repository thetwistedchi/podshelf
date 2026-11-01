import logging
import threading
from datetime import datetime, timedelta, timezone

from . import config, db, downloader, feeds

log = logging.getLogger("podshelf.scheduler")


def _due(p) -> bool:
    if not p["last_checked"]:
        return True
    try:
        last = datetime.fromisoformat(p["last_checked"])
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last >= timedelta(hours=max(1, p["check_hours"]))


def _loop():
    tick = 0
    while True:
        try:
            for p in db.query("SELECT * FROM podcasts WHERE active=1"):
                if _due(p):
                    feeds.refresh_podcast(p["id"])
            if tick % 60 == 0:  # roughly hourly housekeeping
                downloader.verify_files()
        except Exception:
            log.exception("scheduler loop")
        tick += 1
        threading.Event().wait(config.SCHEDULER_TICK)


def start():
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()
