"""Parse Otter.ai exports (TXT, SRT, DOCX, text PDF) and pasted text into a
canonical transcript.

Canonical form (stored in Lecture.transcript_json):
{
  "format": "otter_txt" | "srt" | "plain" | ...,
  "has_timestamps": bool,
  "has_speakers": bool,
  "duration_seconds": float | None,
  "word_count": int,
  "segments": [ {"idx": 0, "pid": "P1", "speaker": "...", "start": 12.0 | None,
                 "end": 30.0 | None, "text": "..."} ],
  "sections": [...]   # only for Otter summaries (Summary / Outline / Action items)
}

Nothing is invented: when timestamps are absent `start`/`end` are None and the
paragraph id ("P12") is the citation unit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

TIME_RE = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
# "Speaker 1  0:12", "John Smith 1:02:33", "Speaker 2 (Instructor)  12:01"
OTTER_HEADER_RE = re.compile(rf"^\s*(?P<speaker>[^\n]{{1,80}}?)\s{{1,}}(?P<time>{TIME_RE})\s*$")
# Lines that are only a timestamp, e.g. "12:34" or "[00:12:34]" or "(1:02:03)"
BARE_TIME_RE = re.compile(rf"^\s*[\[\(]?(?P<time>{TIME_RE})[\]\)]?\s*$")
# Inline leading timestamp: "[00:12:34] Speaker: text" or "00:12 text"
INLINE_TIME_RE = re.compile(rf"^\s*[\[\(]?(?P<time>{TIME_RE})[\]\)]?\s*[-–:]?\s*(?P<rest>.+)$")
SPEAKER_COLON_RE = re.compile(r"^(?P<speaker>[A-Z][\w .'\-]{0,40}?):\s+(?P<rest>.+)$")

SUMMARY_HEADINGS = {"summary", "outline", "action items", "keywords", "key takeaways",
                    "notes", "transcript", "chapters", "key points", "takeaways"}


@dataclass
class Segment:
    idx: int
    pid: str
    speaker: str | None
    start: float | None
    end: float | None
    text: str


@dataclass
class ParsedTranscript:
    format: str
    segments: list[Segment] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    title_hint: str | None = None
    date_hint: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def has_timestamps(self) -> bool:
        return any(s.start is not None for s in self.segments)

    @property
    def has_speakers(self) -> bool:
        return any(s.speaker for s in self.segments)

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)

    @property
    def duration_seconds(self) -> float | None:
        times = [s.end if s.end is not None else s.start for s in self.segments if s.start is not None]
        return max(times) if times else None

    def to_json(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "has_timestamps": self.has_timestamps,
            "has_speakers": self.has_speakers,
            "duration_seconds": self.duration_seconds,
            "word_count": self.word_count,
            "segments": [asdict(s) for s in self.segments],
            "sections": self.sections,
            "title_hint": self.title_hint,
            "date_hint": self.date_hint,
            "warnings": self.warnings,
        }


def parse_timestamp(s: str) -> float | None:
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _finalize(segments: list[Segment]) -> list[Segment]:
    """Assign ids, fill `end` from the next segment's start, drop empties."""
    out: list[Segment] = []
    for s in segments:
        s.text = re.sub(r"[ \t]+", " ", s.text).strip()
        if not s.text:
            continue
        out.append(s)
    for i, s in enumerate(out):
        s.idx = i
        s.pid = f"P{i + 1}"
        if s.end is None and s.start is not None:
            nxt = next((o.start for o in out[i + 1:] if o.start is not None), None)
            s.end = nxt
    return out


# ---------------- SRT ----------------

SRT_BLOCK_RE = re.compile(
    r"(?P<n>\d+)\s*\n(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})[^\n]*\n(?P<text>(?:.+\n?)+?)(?:\n\s*\n|\Z)",
    re.MULTILINE,
)


def parse_srt(text: str) -> ParsedTranscript:
    pt = ParsedTranscript(format="srt")
    raw: list[Segment] = []
    for m in SRT_BLOCK_RE.finditer(text.replace("\r\n", "\n") + "\n\n"):
        body = " ".join(line.strip() for line in m.group("text").splitlines())
        body = re.sub(r"<[^>]+>", "", body)
        speaker = None
        sm = SPEAKER_COLON_RE.match(body)
        if sm:
            speaker, body = sm.group("speaker"), sm.group("rest")
        raw.append(Segment(0, "", speaker, parse_timestamp(m.group("start")), parse_timestamp(m.group("end")), body))
    # Merge consecutive cues from the same speaker into paragraphs (~ up to 60 s)
    merged: list[Segment] = []
    for s in raw:
        if merged and merged[-1].speaker == s.speaker and s.start is not None and merged[-1].start is not None \
                and s.start - merged[-1].start < 45 and len(merged[-1].text) < 900:
            merged[-1].text += " " + s.text
            merged[-1].end = s.end
        else:
            merged.append(s)
    pt.segments = _finalize(merged)
    if not pt.segments:
        pt.warnings.append("No SRT cues recognised; falling back to plain text parsing.")
        return parse_plain(text)
    return pt


# ---------------- Otter TXT / pasted text ----------------

def _is_summary_heading(line: str) -> bool:
    l = line.strip().strip(":").lower()
    return l in SUMMARY_HEADINGS or (len(l) < 40 and l.endswith("summary"))


def parse_plain(text: str) -> ParsedTranscript:
    """Handles Otter TXT exports, Otter 'summary' exports, and arbitrary pasted text."""
    text = text.replace("\r\n", "\n").replace(" ", " ")
    lines = text.split("\n")
    pt = ParsedTranscript(format="plain")
    segments: list[Segment] = []
    sections: list[dict[str, Any]] = []
    cur_section: dict[str, Any] | None = None

    cur_speaker: str | None = None
    cur_time: float | None = None
    buf: list[str] = []
    header_seen = False

    def flush():
        nonlocal buf
        body = " ".join(x.strip() for x in buf if x.strip())
        if body:
            segments.append(Segment(0, "", cur_speaker, cur_time, None, body))
        buf = []

    # Otter TXT files often begin with the title and a date line.
    first_nonblank = [l for l in lines[:6] if l.strip()]
    if first_nonblank and not OTTER_HEADER_RE.match(first_nonblank[0]) and len(first_nonblank[0]) < 120:
        maybe_title = first_nonblank[0].strip()
        if not INLINE_TIME_RE.match(maybe_title) and not _is_summary_heading(maybe_title):
            pt.title_hint = maybe_title
    for l in first_nonblank[:4]:
        if re.search(r"\b(20\d{2})\b", l) and re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2}/\d{1,2})", l, re.I):
            pt.date_hint = l.strip()
            break

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            # Blank line: paragraph boundary when there is no speaker/time structure
            if buf and not header_seen:
                flush()
            continue
        if _is_summary_heading(stripped) and (i == 0 or not lines[i - 1].strip()):
            flush()
            cur_section = {"heading": stripped.strip(":"), "lines": []}
            sections.append(cur_section)
            cur_speaker, cur_time = None, None
            if stripped.lower().startswith("transcript"):
                cur_section = None
            continue
        if cur_section is not None:
            cur_section["lines"].append(stripped)
            continue
        m = OTTER_HEADER_RE.match(line)
        if m and len(m.group("speaker").split()) <= 6 and not m.group("speaker").endswith((".", ",", "?")):
            flush()
            header_seen = True
            cur_speaker = m.group("speaker").strip()
            cur_time = parse_timestamp(m.group("time"))
            continue
        m = BARE_TIME_RE.match(line)
        if m:
            flush()
            header_seen = True
            cur_time = parse_timestamp(m.group("time"))
            continue
        m = INLINE_TIME_RE.match(line)
        if m and (m.group("rest")[:1].isupper() or m.group("rest")[:1] in "\"'("):
            flush()
            header_seen = True
            cur_time = parse_timestamp(m.group("time"))
            rest = m.group("rest")
            sm = SPEAKER_COLON_RE.match(rest)
            if sm:
                cur_speaker, rest = sm.group("speaker"), sm.group("rest")
            buf.append(rest)
            continue
        sm = SPEAKER_COLON_RE.match(stripped)
        if sm and len(sm.group("speaker").split()) <= 4 and not header_seen:
            flush()
            cur_speaker = sm.group("speaker")
            buf.append(sm.group("rest"))
            continue
        buf.append(stripped)
    flush()

    # Drop the title/date lines if we captured them as a segment
    if segments and pt.title_hint and segments[0].text.startswith(pt.title_hint) and len(segments[0].text) < 200:
        segments.pop(0)

    pt.segments = _finalize(segments)
    pt.sections = [{"heading": s["heading"], "text": "\n".join(s["lines"])} for s in sections if s["lines"]]
    if pt.has_timestamps:
        pt.format = "otter_txt"
    if pt.sections and not pt.segments:
        # Otter summary export: expose the summary text as citable paragraphs so limited
        # notes can still be produced, clearly labelled as summary-derived.
        pt.format = "otter_summary"
        summary_segs = [Segment(0, "", None, None, None, f"[{sec['heading']}] {line}")
                        for sec in pt.sections for line in sec["text"].splitlines() if line.strip()]
        pt.segments = _finalize(summary_segs)
        pt.warnings.append("This file is an Otter summary (Summary/Outline/Action items), not a spoken transcript.")
    # Guard against very long untimed blobs: split into ~120-word paragraphs for citation
    if not pt.has_timestamps:
        resplit: list[Segment] = []
        for s in pt.segments:
            words = s.text.split()
            if len(words) <= 220:
                resplit.append(s)
                continue
            sentences = re.split(r"(?<=[.!?])\s+", s.text)
            chunk: list[str] = []
            count = 0
            for sent in sentences:
                chunk.append(sent)
                count += len(sent.split())
                if count >= 120:
                    resplit.append(Segment(0, "", s.speaker, None, None, " ".join(chunk)))
                    chunk, count = [], 0
            if chunk:
                resplit.append(Segment(0, "", s.speaker, None, None, " ".join(chunk)))
        pt.segments = _finalize(resplit)
    return pt


# ---------------- DOCX / PDF ----------------

def extract_docx_text(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return (text, images). Images: [{"name", "bytes", "paragraph_index"}]."""
    import docx  # python-docx
    from docx.oxml.ns import qn

    document = docx.Document(str(path))
    lines: list[str] = []
    images: list[dict[str, Any]] = []
    rels = document.part.rels
    for p_idx, para in enumerate(document.paragraphs):
        txt = para.text
        # inline images in this paragraph
        for blip in para._p.iter(qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            if rid and rid in rels:
                part = rels[rid].target_part
                images.append({
                    "name": Path(part.partname).name,
                    "bytes": part.blob,
                    "paragraph_index": p_idx,
                    "content_type": part.content_type,
                })
                txt += f" [IMAGE:{Path(part.partname).name}]"
        lines.append(txt)
    # tables
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(lines), images


def extract_pdf_text(path: Path) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Return (text, images, info). Flags scanned/image-only PDFs."""
    import fitz  # pymupdf

    doc = fitz.open(str(path))
    pages_text: list[str] = []
    images: list[dict[str, Any]] = []
    for pno, page in enumerate(doc):
        t = page.get_text("text")
        pages_text.append(t)
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha >= 4:  # CMYK -> RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                if pix.width < 64 or pix.height < 64:
                    continue
                images.append({"name": f"page{pno + 1}_img{xref}.png", "bytes": pix.tobytes("png"),
                               "page": pno + 1, "content_type": "image/png"})
            except Exception:
                continue
    text = "\n".join(pages_text)
    info = {"pages": len(doc), "text_chars": len(text.strip()), "image_only": len(text.strip()) < 50 * max(1, len(doc))}
    return text, images, info


def parse_file(path: Path, pasted_text: str | None = None) -> tuple[ParsedTranscript, list[dict[str, Any]]]:
    """Dispatch on extension. Returns (transcript, extracted_images)."""
    images: list[dict[str, Any]] = []
    if pasted_text is not None:
        text = pasted_text
        ext = ".txt"
    else:
        ext = path.suffix.lower()
        if ext == ".docx":
            text, images = extract_docx_text(path)
        elif ext == ".pdf":
            text, images, info = extract_pdf_text(path)
            if info["image_only"]:
                pt = ParsedTranscript(format="pdf_image_only")
                pt.warnings.append("This PDF has little or no extractable text (scanned/image-only). "
                                   "OCR is not performed in this release; export the transcript as TXT/DOCX from Otter instead.")
                return pt, images
        else:
            raw = path.read_bytes()
            for enc in ("utf-8-sig", "utf-16", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = raw.decode("utf-8", errors="replace")
    if ext == ".srt" or re.search(r"^\d+\s*\n\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->", text, re.M):
        pt = parse_srt(text)
    elif ext == ".vtt" or text.lstrip().startswith("WEBVTT"):
        body = re.sub(r"^WEBVTT.*?\n\n", "", text.lstrip(), count=1, flags=re.S)
        # VTT -> SRT-ish numbering
        blocks = [b for b in re.split(r"\n\s*\n", body) if "-->" in b]
        srt = "\n\n".join(f"{i + 1}\n{b}" for i, b in enumerate(blocks))
        pt = parse_srt(srt)
        pt.format = "vtt"
    else:
        pt = parse_plain(text)
    if ext == ".docx":
        pt.format = "docx_" + pt.format
    elif ext == ".pdf":
        pt.format = "pdf_" + pt.format
    return pt, images


# ---------------- assessment ----------------

def assess_source(pt: ParsedTranscript) -> dict[str, Any]:
    """Classify as full transcript vs Otter summary and judge sufficiency."""
    wc = pt.word_count
    dur = pt.duration_seconds
    summary_words = sum(len(s["text"].split()) for s in pt.sections)
    kind = "full_transcript"
    reasons: list[str] = []
    if pt.format.endswith("otter_summary"):
        kind = "otter_summary"
        reasons.append("Only an Otter summary was supplied (Summary/Outline/Action items). Comprehensive notes need the full transcript export.")
    elif pt.sections and wc < 400:
        kind = "otter_summary"
        reasons.append("Only summary/outline sections were found - no spoken transcript paragraphs.")
    elif pt.sections and wc < summary_words * 3:
        kind = "mixed"
        reasons.append("Contains both an Otter summary and a short transcript portion.")
    if wc == 0:
        kind = "insufficient"
    elif kind == "otter_summary":
        pass
        reasons.append("No text could be extracted.")
    elif wc < 1500:
        reasons.append(f"Only {wc} words of transcript. A full lecture usually produces 20,000+ words; "
                       "notes will be limited to what was supplied.")
    if dur and wc:
        wpm = wc / (dur / 60)
        if wpm < 60:
            reasons.append(f"Low speaking density ({wpm:.0f} words/min): the transcript may be partial or heavily trimmed.")
    if not pt.has_timestamps:
        reasons.append("No timestamps found: notes will cite paragraph numbers (P1, P2, …) instead of times.")
    if not pt.has_speakers:
        reasons.append("No speaker labels found: instructor vs student turns cannot be distinguished automatically.")
    sufficient = kind == "full_transcript" and wc >= 1500
    if kind == "full_transcript" and wc < 1500:
        sufficient = False
    return {
        "kind": kind,
        "sufficient_for_comprehensive_notes": sufficient,
        "word_count": wc,
        "duration_seconds": dur,
        "has_timestamps": pt.has_timestamps,
        "has_speakers": pt.has_speakers,
        "segment_count": len(pt.segments),
        "summary_sections": [s["heading"] for s in pt.sections],
        "notes": reasons + pt.warnings,
    }


def transcript_to_text(tj: dict[str, Any], with_ids: bool = True) -> str:
    """Render canonical transcript as the text the model sees (with citation ids)."""
    out: list[str] = []
    for s in tj.get("segments", []):
        tag = s["pid"]
        if s.get("start") is not None:
            tag += f" @{format_timestamp(s['start'])}"
        spk = f" {s['speaker']}:" if s.get("speaker") else ""
        out.append(f"[{tag}]{spk} {s['text']}" if with_ids else s["text"])
    return "\n".join(out)
