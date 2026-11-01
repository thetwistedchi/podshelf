import re
import unicodedata
from datetime import datetime

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")

PODCAST_FIELDS = ["podcast", "author"]
EPISODE_FIELDS = ["podcast", "title", "date", "year", "month", "day", "num", "author"]


def safe(name: str, max_len: int = 150) -> str:
    """Filesystem-safe name that works on SMB/NFS and in Plex/Emby."""
    name = unicodedata.normalize("NFC", name or "")
    name = _BAD.sub(" ", name)
    name = _WS.sub(" ", name).strip().strip(".")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .-")
    return name or "untitled"


class _Safe(dict):
    def __missing__(self, key):
        return ""


def _render(fmt: str, values: dict) -> str:
    try:
        out = fmt.format_map(_Safe(values))
    except (ValueError, IndexError):
        out = values.get("title") or values.get("podcast") or ""
    out = re.sub(r"^[\s\-_.]+|[\s\-_.]+$", "", out)  # trim leftover separators
    return safe(out)


def podcast_folder(fmt: str, title: str, author: str = "") -> str:
    return _render(fmt or "{podcast}", {"podcast": title or "", "author": author or ""})


def episode_basename(fmt: str, podcast: str, title: str, pub_date: str | None,
                     num: int | None = None, author: str = "") -> str:
    d = None
    if pub_date:
        try:
            d = datetime.fromisoformat(pub_date)
        except ValueError:
            d = None
    values = {
        "podcast": podcast or "",
        "title": title or "",
        "author": author or "",
        "date": d.strftime("%Y-%m-%d") if d else "",
        "year": d.strftime("%Y") if d else "",
        "month": d.strftime("%m") if d else "",
        "day": d.strftime("%d") if d else "",
        "num": f"{num:03d}" if num else "",
    }
    return _render(fmt or "{date} - {title}", values)


def sample(podcast_fmt: str, episode_fmt: str) -> str:
    folder = podcast_folder(podcast_fmt, "Kein Mucks", "Bastian Pastewka")
    ep = episode_basename(episode_fmt, "Kein Mucks", "Der Fall Mona Lisa",
                          "2026-10-29T06:00:00+00:00", 214, "Bastian Pastewka")
    return f"{folder}/{ep}.mp3"
