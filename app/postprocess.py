"""Chapters, ad skipping (trim + ad chapters) and length checks."""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

import mutagen
from mutagen.id3 import CHAP, CTOC, CTOCFlags, ID3, TIT2, ID3NoHeaderError

log = logging.getLogger("podshelf.post")
FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------- parsing
def parse_time(v) -> float | None:
    """'01:02:03.5', '62:03', '3723', 3723 -> seconds."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            return float(v)
        parts = [float(x) for x in str(v).strip().split(":")]
        secs = 0.0
        for p in parts:
            secs = secs * 60 + p
        return secs
    except ValueError:
        return None


def audio_length(path: str) -> int | None:
    try:
        f = mutagen.File(path)
        if f is not None and f.info and f.info.length:
            return int(f.info.length)
    except Exception:
        pass
    return None


def chapters_from_json(data) -> list[dict]:
    """Podcasting 2.0 JSON chapters."""
    out = []
    for ch in (data or {}).get("chapters", []):
        if ch.get("toc") is False:
            continue
        t = parse_time(ch.get("startTime"))
        if t is not None:
            out.append({"start": t, "title": (ch.get("title") or "").strip()})
    return sorted(out, key=lambda c: c["start"])


def chapters_from_file(path: str) -> list[dict]:
    if not path.lower().endswith(".mp3"):
        return []
    try:
        tags = ID3(path)
    except Exception:
        return []
    out = []
    for ch in tags.getall("CHAP"):
        title = ""
        for sub in ch.sub_frames.values():
            if sub.FrameID == "TIT2":
                title = str(sub.text[0])
        out.append({"start": ch.start_time / 1000, "title": title})
    return sorted(out, key=lambda c: c["start"])


# ---------------------------------------------------------------- ad skipping
def ad_matcher(keywords: str):
    words = [w.strip() for w in (keywords or "").split(",") if w.strip()]
    if not words:
        return lambda title: False
    rx = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)
    return lambda title: bool(rx.search(title or ""))


def plan_cuts(length: float, chapters: list[dict], trim_start: int, trim_end: int,
              cut_chapters: bool, keywords: str) -> list[tuple[float, float]]:
    """Return the segments to KEEP, or [] if nothing has to be cut."""
    remove = []
    if trim_start > 0:
        remove.append((0.0, float(trim_start)))
    if trim_end > 0:
        remove.append((max(0.0, length - trim_end), length))
    if cut_chapters and chapters:
        is_ad = ad_matcher(keywords)
        for i, ch in enumerate(chapters):
            end = chapters[i + 1]["start"] if i + 1 < len(chapters) else length
            if is_ad(ch["title"]) and end > ch["start"]:
                remove.append((ch["start"], end))
    if not remove:
        return []
    remove.sort()
    merged = [list(remove[0])]
    for s, e in remove[1:]:
        if s <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    keep, pos = [], 0.0
    for s, e in merged:
        if s - pos > 1:
            keep.append((pos, s))
        pos = max(pos, e)
    if length - pos > 1:
        keep.append((pos, length))
    removed = length - sum(e - s for s, e in keep)
    if removed < 1 or not keep:
        return []  # nothing worth cutting, or everything would be gone
    return keep


def shift_chapters(chapters: list[dict], keep: list[tuple[float, float]], is_ad) -> list[dict]:
    """Map chapter times onto the cut file, dropping removed chapters."""
    out = []
    for ch in chapters:
        if is_ad(ch["title"]):
            continue
        offset, new_start = 0.0, None
        for s, e in keep:
            if s <= ch["start"] < e:
                new_start = offset + (ch["start"] - s)
                break
            if ch["start"] < s:  # chapter starts inside a removed part: snap to next kept part
                new_start = offset
                break
            offset += e - s
        if new_start is not None and not (out and abs(out[-1]["start"] - new_start) < 1):
            out.append({"start": round(new_start, 2), "title": ch["title"]})
    return out


def cut(path: str, keep: list[tuple[float, float]]) -> bool:
    """Keep only the given segments (stream copy, no re-encode)."""
    if not FFMPEG:
        log.warning("ffmpeg not found, cannot cut %s", path)
        return False
    ext = os.path.splitext(path)[1]
    tmpdir = tempfile.mkdtemp(prefix="podshelf-cut-", dir=os.path.dirname(path))
    try:
        parts = []
        for i, (s, e) in enumerate(keep):
            part = os.path.join(tmpdir, f"p{i}{ext}")
            _ff(["-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", path, "-map", "0:a", "-c", "copy",
                 "-map_metadata", "-1", "-map_chapters", "-1", part])
            parts.append(part)
        listfile = os.path.join(tmpdir, "list.txt")
        with open(listfile, "w") as fh:
            fh.writelines(f"file '{p}'\n" for p in parts)
        out = os.path.join(tmpdir, "out" + ext)
        _ff(["-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", out])
        os.replace(out, path)
        return True
    except Exception as e:
        log.warning("cutting %s failed: %s", path, e)
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _ff(args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True, timeout=1800)


# ---------------------------------------------------------------- chapter embedding
def embed_chapters(path: str, chapters: list[dict], length: int | None):
    if not chapters:
        return
    length_ms = int((length or chapters[-1]["start"] + 1) * 1000)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("CHAP")
        tags.delall("CTOC")
        ids = []
        for i, ch in enumerate(chapters):
            start = int(ch["start"] * 1000)
            end = int(chapters[i + 1]["start"] * 1000) if i + 1 < len(chapters) else length_ms
            cid = f"chp{i}"
            ids.append(cid)
            tags.add(CHAP(element_id=cid, start_time=start, end_time=max(end, start + 1),
                          sub_frames=[TIT2(encoding=3, text=ch["title"] or f"Chapter {i + 1}")]))
        tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                      child_element_ids=ids, sub_frames=[TIT2(encoding=3, text="Chapters")]))
        tags.save(path, v2_version=3)
    elif ext in (".m4a", ".m4b", ".mp4") and FFMPEG:
        meta = ";FFMETADATA1\n"
        for i, ch in enumerate(chapters):
            start = int(ch["start"] * 1000)
            end = int(chapters[i + 1]["start"] * 1000) if i + 1 < len(chapters) else length_ms
            title = (ch["title"] or f"Chapter {i + 1}").replace("=", r"\=").replace(";", r"\;") \
                .replace("#", r"\#").replace("\n", " ")
            meta += f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={max(end, start + 1)}\ntitle={title}\n"
        fd, metafile = tempfile.mkstemp(suffix=".txt", dir=os.path.dirname(path))
        out = path + ".chap" + ext
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(meta)
            _ff(["-i", path, "-i", metafile, "-map", "0", "-map_metadata", "0", "-map_chapters", "1",
                 "-c", "copy", out])
            os.replace(out, path)
        except Exception as e:
            log.warning("chapters for %s failed: %s", path, e)
        finally:
            for f in (metafile, out):
                if os.path.exists(f):
                    os.remove(f)


def dumps(chapters):
    return json.dumps(chapters, ensure_ascii=False) if chapters else None


def loads(text):
    try:
        return json.loads(text) if text else []
    except ValueError:
        return []
