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
    "strip_date_tag": "1",
    "force_podcast_cover": "0",
}

MAX_WORKERS = 8
