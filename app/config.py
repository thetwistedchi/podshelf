import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PODCAST_DIR = os.environ.get("PODCAST_DIR", "/podcasts")
DB_PATH = os.path.join(DATA_DIR, "podshelf.db")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # empty = login disabled
SECRET_KEY = os.environ.get("SECRET_KEY", "")

USER_AGENT = "Podshelf/1.0 (+self-hosted podcast downloader)"
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "60"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
SCHEDULER_TICK = int(os.environ.get("SCHEDULER_TICK", "60"))

DEFAULT_SETTINGS = {
    "episode_format": "{date} - {title}",
    "podcast_format": "{podcast}",
    "default_keep": "10",
    "default_check_hours": "6",
    "default_initial": "10",
    "default_max_new": "3",
    "parallel_downloads": "3",
    "force_podcast_cover": "0",
    # download window (hours 0-23, empty = always) and bandwidth (KB/s, 0 = unlimited)
    "window_start": "",
    "window_end": "",
    "bandwidth_kbps": "0",
    # default ad chapter keywords (whole words, case-insensitive)
    "ad_chapters_all": "0",   # remove ad chapters for every podcast
    "ad_keywords": "werbung, anzeige, sponsor, sponsored, ad, ads, advertisement, promo, commercial",
    # media server sync
    "sync_minutes": "15",
    "plex_url": "", "plex_token": "", "plex_library": "",
    "emby_url": "", "emby_key": "", "emby_user": "", "emby_library": "",
}

# settings that are checkboxes in the UI
BOOL_SETTINGS = {"force_podcast_cover", "ad_chapters_all"}

# bump to re-tag every downloaded file once after an update that changes tags
TAG_VERSION = "2"
# settings that must be non-negative integers
INT_SETTINGS = {"default_keep", "default_initial", "default_check_hours", "default_max_new",
                "parallel_downloads", "bandwidth_kbps", "sync_minutes"}
# settings never shown back in the UI
SECRET_SETTINGS = {"plex_token", "emby_key"}

MAX_WORKERS = 8
