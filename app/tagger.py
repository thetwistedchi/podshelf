"""Tags written so Plex (Music library) and Emby show one album per podcast,
episodes sorted chronologically by track number, each with artwork."""
import base64
import logging
import os
import re

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (ID3, APIC, COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK,
                         ID3NoHeaderError)
from mutagen.mp4 import MP4, MP4Cover

log = logging.getLogger("podshelf.tagger")
_TAGS = re.compile(r"<[^>]+>")


def _plain(text: str, limit=900) -> str:
    return _TAGS.sub("", text or "").strip()[:limit]


def _mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def tag_file(path: str, *, album: str, title: str, track: int, year: str | None,
             description: str = "", cover: bytes | None = None, strip_date=True):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        _mp3(path, album, title, track, year, description, cover, strip_date)
    elif ext in (".m4a", ".m4b", ".mp4", ".aac") and _is_mp4(path):
        _mp4(path, album, title, track, year, description, cover, strip_date)
    elif ext == ".flac":
        _flac(path, album, title, track, year, cover, strip_date)
    elif ext in (".ogg", ".opus", ".oga"):
        _ogg(path, album, title, track, year, cover, strip_date)
    else:
        f = mutagen.File(path)
        if ext == ".aac" or (f is not None and type(f).__name__ in ("MP3", "EasyMP3")):
            _mp3(path, album, title, track, year, description, cover, strip_date)
        else:
            log.info("no tagging for %s", path)


def _is_mp4(path):
    with open(path, "rb") as fh:
        return fh.read(12)[4:8] == b"ftyp"


def _mp3(path, album, title, track, year, desc, cover, strip_date):
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
    if year and not strip_date:
        t.add(TDRC(encoding=3, text=year))
    if desc:
        t.delall("COMM")
        t.add(COMM(encoding=3, lang="eng", desc="", text=_plain(desc)))
    if cover:
        t.delall("APIC")
        t.add(APIC(encoding=3, mime=_mime(cover), type=3, desc="Cover", data=cover))
    t.save(path, v2_version=3)


def _mp4(path, album, title, track, year, desc, cover, strip_date):
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
    if strip_date:
        t.pop("\xa9day", None)
    elif year:
        t["\xa9day"] = [year]
    if desc:
        t["desc"] = [_plain(desc, 250)]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if _mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        t["covr"] = [MP4Cover(cover, imageformat=fmt)]
    t.save()


def _vorbis_common(t, album, title, track, year, strip_date):
    t["album"] = album
    t["artist"] = album
    t["albumartist"] = album
    t["title"] = title
    t["tracknumber"] = str(track)
    t["genre"] = "Podcast"
    for k in ("discnumber", "compilation"):
        if k in t:
            del t[k]
    if strip_date:
        if "date" in t:
            del t["date"]
    elif year:
        t["date"] = year


def _picture(cover):
    pic = Picture()
    pic.type = 3
    pic.mime = _mime(cover)
    pic.desc = "Cover"
    pic.data = cover
    return pic


def _flac(path, album, title, track, year, cover, strip_date):
    t = FLAC(path)
    _vorbis_common(t, album, title, track, year, strip_date)
    if cover:
        t.clear_pictures()
        t.add_picture(_picture(cover))
    t.save()


def _ogg(path, album, title, track, year, cover, strip_date):
    t = mutagen.File(path)
    if t is None:
        return
    if t.tags is None:
        t.add_tags()
    _vorbis_common(t, album, title, track, year, strip_date)
    if cover:
        t["metadata_block_picture"] = [base64.b64encode(_picture(cover).write()).decode()]
    t.save()
