"""Import workflow: create lectures from uploaded/pasted transcripts plus
optional attachments (slides, screenshots, recordings)."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from . import db as dbm
from .parsers.transcript import assess_source, parse_file
from .storage import lecture_dir, store_image, store_source

TRANSCRIPT_EXTS = {".txt", ".srt", ".vtt", ".docx", ".pdf", ".md"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SLIDE_EXTS = {".pdf", ".pptx", ".key"}
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".m4v"}


def guess_title(filename: str, hint: str | None) -> str:
    if hint and 3 < len(hint) < 120:
        return hint
    stem = Path(filename).stem
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s+\d{4}[\d\- :T]*$", "", stem)
    return stem.strip() or "Untitled lecture"


def guess_week(text: str) -> int | None:
    m = re.search(r"\bweek\s*(\d{1,2})\b", text, re.I)
    return int(m.group(1)) if m else None


def guess_date(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(20\d{2})", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    m = re.search(r"\b([A-Za-z]{3,9})\.? (\d{1,2}),? (20\d{2})", text)
    if m and m.group(1)[:3].lower() in months:
        try:
            return date(int(m.group(3)), months.index(m.group(1)[:3].lower()) + 1, int(m.group(2))).isoformat()
        except ValueError:
            return None
    return None


def get_or_create_course(db, name: str) -> dbm.Course:
    name = (name or "Untitled course").strip()
    c = db.query(dbm.Course).filter(dbm.Course.name == name).first()
    if not c:
        c = dbm.Course(name=name)
        db.add(c)
        db.flush()
    return c


def create_lecture_from_transcript(db, *, course_name: str, title: str | None, week: int | None, lecture_date: str | None,
                                   filename: str | None, data: bytes | None, pasted_text: str | None) -> dbm.Lecture:
    course = get_or_create_course(db, course_name)
    lec = dbm.Lecture(course_id=course.id, title=title or "Untitled lecture", week=week, lecture_date=lecture_date)
    db.add(lec)
    db.flush()
    ldir = lecture_dir(lec.id)
    images: list[dict[str, Any]] = []
    if pasted_text is not None:
        rel, path = store_source(lec.id, "pasted_transcript.txt", pasted_text.encode("utf-8"))
        parsed, images = parse_file(path, pasted_text=pasted_text)
        db.add(dbm.SourceFile(lecture_id=lec.id, kind="pasted", original_name="pasted text", stored_path=rel,
                              mime="text/plain", size_bytes=len(pasted_text.encode())))
        src_name = "pasted text"
    else:
        assert filename and data is not None
        rel, path = store_source(lec.id, filename, data)
        parsed, images = parse_file(path)
        db.add(dbm.SourceFile(lecture_id=lec.id, kind="transcript", original_name=filename, stored_path=rel,
                              size_bytes=len(data), meta={"format": parsed.format}))
        src_name = filename
    tj = parsed.to_json()
    lec.transcript_json = tj
    lec.source_assessment = assess_source(parsed)
    lec.source_kind = lec.source_assessment["kind"]
    if not title:
        lec.title = guess_title(src_name, parsed.title_hint)
    if week is None:
        lec.week = guess_week(lec.title) or guess_week(src_name)
    if not lecture_date:
        lec.lecture_date = guess_date(parsed.date_hint) or guess_date(src_name)
    # embedded images from DOCX/PDF transcripts: associated with paragraph/page, never a timestamp
    for im in images:
        rel_img, _ = store_image(lec.id, im["name"], im["bytes"])
        db.add(dbm.SourceFile(lecture_id=lec.id, kind="image", original_name=im["name"], stored_path=rel_img,
                              mime=im.get("content_type"), size_bytes=len(im["bytes"]),
                              meta={"from": src_name, "page": im.get("page"), "paragraph_index": im.get("paragraph_index")}))
    last = db.query(dbm.Lecture).filter(dbm.Lecture.course_id == course.id).count()
    lec.order_index = last
    db.flush()
    return lec


def attach_file(db, lecture: dbm.Lecture, filename: str, data: bytes) -> list[dbm.SourceFile]:
    """Attach slides / screenshots / recordings. PDF slides are rendered page by page."""
    ext = Path(filename).suffix.lower()
    out: list[dbm.SourceFile] = []
    if ext in IMAGE_EXTS:
        rel, _ = store_image(lecture.id, filename, data)
        sf = dbm.SourceFile(lecture_id=lecture.id, kind="screenshot", original_name=filename, stored_path=rel,
                            size_bytes=len(data), meta={"timestamp": None})
        db.add(sf)
        out.append(sf)
    elif ext == ".pdf":
        rel, path = store_source(lecture.id, filename, data)
        sf = dbm.SourceFile(lecture_id=lecture.id, kind="slides", original_name=filename, stored_path=rel, size_bytes=len(data))
        db.add(sf)
        out.append(sf)
        import fitz
        doc = fitz.open(str(path))
        for pno, page in enumerate(doc):
            if pno >= 150:
                break
            pix = page.get_pixmap(dpi=110)
            rel_img, _ = store_image(lecture.id, f"{Path(filename).stem}_p{pno + 1:03d}.png", pix.tobytes("png"))
            db.add(dbm.SourceFile(lecture_id=lecture.id, kind="slides_page", original_name=f"{filename} p.{pno + 1}",
                                  stored_path=rel_img, mime="image/png", size_bytes=0,
                                  meta={"page": pno + 1, "from": filename, "text": page.get_text("text")[:4000]}))
    elif ext in MEDIA_EXTS:
        rel, _ = store_source(lecture.id, filename, data)
        sf = dbm.SourceFile(lecture_id=lecture.id, kind="recording", original_name=filename, stored_path=rel, size_bytes=len(data),
                            meta={"note": "Stored for the video-processing release; not used for transcript notes."})
        db.add(sf)
        out.append(sf)
    else:
        rel, _ = store_source(lecture.id, filename, data)
        sf = dbm.SourceFile(lecture_id=lecture.id, kind="other", original_name=filename, stored_path=rel, size_bytes=len(data))
        db.add(sf)
        out.append(sf)
    db.flush()
    return out
