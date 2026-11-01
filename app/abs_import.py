"""Import podcasts and their schedule/retention settings from Audiobookshelf."""
import re

import httpx

from . import feeds


def _hours_from_cron(cron: str | None, default: int) -> int:
    if not cron:
        return default
    parts = cron.split()
    if len(parts) >= 2:
        m = re.fullmatch(r"\*/(\d+)", parts[1])
        if m:
            return int(m.group(1))
        if parts[1] == "*":
            return 1
        if "," in parts[1]:
            return max(1, 24 // len(parts[1].split(",")))
        if parts[1].isdigit():
            return 24
    return default


def import_from_abs(base_url: str, api_key: str, default_hours: int) -> dict:
    base = base_url.rstrip("/")
    h = {"Authorization": f"Bearer {api_key}"}
    added, skipped, errors = 0, 0, []
    with httpx.Client(timeout=60, headers=h, follow_redirects=True) as c:
        libs = c.get(f"{base}/api/libraries").raise_for_status().json()["libraries"]
        for lib in (l for l in libs if l.get("mediaType") == "podcast"):
            items = c.get(f"{base}/api/libraries/{lib['id']}/items", params={"limit": 0}) \
                     .raise_for_status().json()["results"]
            for it in items:
                try:
                    item = c.get(f"{base}/api/items/{it['id']}").raise_for_status().json()
                    media = item["media"]
                    url = media["metadata"].get("feedUrl")
                    if not url:
                        skipped += 1
                        continue
                    from . import db
                    if db.one("SELECT 1 FROM podcasts WHERE feed_url=?", (url,)):
                        skipped += 1
                        continue
                    pid = feeds.add_podcast(
                        url,
                        keep=media.get("maxEpisodesToKeep", 10) or 0,
                        check_hours=_hours_from_cron(media.get("autoDownloadSchedule"), default_hours),
                        max_new=media.get("maxNewEpisodesToDownload", 3) or 0,
                        auto=bool(media.get("autoDownloadEpisodes", True)),
                    )
                    feeds.refresh_async(pid)
                    added += 1
                except Exception as e:
                    errors.append(f"{it.get('id')}: {e}")
    return {"added": added, "skipped": skipped, "errors": errors}
