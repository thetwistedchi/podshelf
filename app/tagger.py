"""Tags written so Plex (Music library) and Emby show one album per podcast,
episodes sorted chronologically by track number, each with artwork and its
release date.

The release date goes into "release date" fields only (ID3 TDRL, MP4/Vorbis
RELEASEDATE), never into the year/recording date (TDRC, ©day, DATE): Plex
groups album tracks by year, so a per-episode year splits one podcast into
several albums. The date is also the start of the file name."""
import base64
import logging
import os
import re

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (ID3, APIC, COMM, TALB, TCON, TDRC, TDRL, TIT2, TPE1, TPE2, TRCK,
                         ID3NoHeaderError)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

log = logging.getLogger("podshelf.tagger")
_TAGS = re.compile(r"<[^>]+>")


def _plain(text: str, limit=900) -> str:
    return _TAGS.sub("", text or "").strip()[:limit]


def _mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def tag_file(path: str, *, album: str, title: str, track: int, date: str | None,
             description: str = "", cover: bytes | None = None):
    """date: release date as YYYY-MM-DD (written to release-date fields when known)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        _mp3(path, album, title, track, date, description, cover)
    elif ext in (".m4a", ".m4b", ".mp4", ".aac") and _is_mp4(path):
        _mp4(path, album, title, track, date, description, cover)
    elif ext == ".flac":
        _flac(path, album, title, track, date, cover)
    elif ext in (".ogg", ".opus", ".oga"):
        _ogg(path, album, title, track, date, cover)
    else:
        f = mutagen.File(path)
        if ext == ".aac" or (f is not None and type(f).__name__ in ("MP3", "EasyMP3")):
            _mp3(path, album, title, track, date, description, cover)
        else:
            log.info("no tagging for %s", path)


def _is_mp4(path):
    with open(path, "rb") as fh:
        return fh.read(12)[4:8] == b"ftyp"


def _mp3(path, album, title, track, date, desc, cover):
    try:
        t = ID3(path)
    except ID3NoHeaderError:
        t = ID3()
    for fid in ("TALB", "TPE1", "TPE2", "TIT2", "TRCK", "TPOS", "TCMP", "TCON", "TSO2", "TSOA",
                "TSOP", "TPE3", "TPE4"):
        t.delall(fid)
    t.add(TALB(encoding=3, text=album))
    t.add(TPE1(encoding=3, text=album))
    t.add(TPE2(encoding=3, text=album))
    t.add(TIT2(encoding=3, text=title))
    t.add(TRCK(encoding=3, text=str(track)))
    t.add(TCON(encoding=3, text="Podcast"))
    for fid in ("TDRC", "TYER", "TDAT", "TIME", "TORY", "TDOR", "TDRL"):
        t.delall(fid)
    if date:
        t.add(TDRL(encoding=3, text=date))   # release time; no TDRC (Plex would split albums by year)
    if desc:
        t.delall("COMM")
        t.add(COMM(encoding=3, lang="eng", desc="", text=_plain(desc)))
    if cover:
        t.delall("APIC")
        t.add(APIC(encoding=3, mime=_mime(cover), type=3, desc="Cover", data=cover))
    t.save(path, v2_version=4)  # v2.3 would cut the full date down to TYER/TDAT


def _mp4(path, album, title, track, date, desc, cover):
    t = MP4(path)
    if t.tags is None:
        t.add_tags()
    t["\xa9alb"] = [album]
    t["\xa9ART"] = [album]
    t["aART"] = [album]
    t["\xa9nam"] = [title]
    t["trkn"] = [(track, 0)]
    t["\xa9gen"] = ["Podcast"]
    for k in ("disk", "cpil", "soaa", "soal", "soar"):
        t.pop(k, None)
    t.pop("\xa9day", None)  # Plex reads this as the year and would split albums
    key = "----:com.apple.iTunes:RELEASEDATE"
    if date:
        t[key] = [MP4FreeForm(date.encode())]
    else:
        t.pop(key, None)
    if desc:
        t["desc"] = [_plain(desc, 250)]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if _mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        t["covr"] = [MP4Cover(cover, imageformat=fmt)]
    t.save()


def _vorbis_common(t, album, title, track, date):
    t["album"] = album
    t["artist"] = album
    t["albumartist"] = album
    t["title"] = title
    t["tracknumber"] = str(track)
    t["genre"] = "Podcast"
    for k in ("discnumber", "compilation"):
        if k in t:
            del t[k]
    if "date" in t:
        del t["date"]  # Plex reads this as the year and would split albums
    if date:
        t["releasedate"] = date
    elif "releasedate" in t:
        del t["releasedate"]


def _picture(cover):
    pic = Picture()
    pic.type = 3
    pic.mime = _mime(cover)
    pic.desc = "Cover"
    pic.data = cover
    return pic


def _flac(path, album, title, track, date, cover):
    t = FLAC(path)
    _vorbis_common(t, album, title, track, date)
    if cover:
        t.clear_pictures()
        t.add_picture(_picture(cover))
    t.save()


def _ogg(path, album, title, track, date, cover):
    t = mutagen.File(path)
    if t is None:
        return
    if t.tags is None:
        t.add_tags()
    _vorbis_common(t, album, title, track, date)
    if cover:
        t["metadata_block_picture"] = [base64.b64encode(_picture(cover).write()).decode()]
    t.save()
