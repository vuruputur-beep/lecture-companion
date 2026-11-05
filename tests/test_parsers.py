from pathlib import Path

import pytest

from app.parsers.transcript import assess_source, parse_file, parse_plain, parse_srt

FIX = Path(__file__).parent / "fixtures"


def test_otter_txt_preserves_speakers_and_timestamps():
    pt, imgs = parse_file(FIX / "week4_rag_otter.txt")
    assert pt.format == "otter_txt"
    assert pt.has_timestamps and pt.has_speakers
    assert pt.segments[0].speaker == "Speaker 1" and pt.segments[0].start == 0.0
    assert pt.segments[1].start is not None
    assert pt.title_hint.startswith("FDE Week 4")
    assert len(pt.segments) > 100
    a = assess_source(pt)
    assert a["kind"] == "full_transcript" and a["sufficient_for_comprehensive_notes"]


def test_srt():
    pt, _ = parse_file(FIX / "week4_rag.srt")
    assert pt.format == "srt" and pt.has_timestamps and pt.has_speakers
    assert pt.segments[0].end is not None


def test_untimed_cites_paragraphs():
    pt, _ = parse_file(FIX / "week3_eval_untimed.txt")
    assert not pt.has_timestamps
    assert pt.segments[0].pid == "P1" and pt.segments[0].start is None
    a = assess_source(pt)
    assert any("paragraph" in n.lower() for n in a["notes"])


def test_summary_detected_as_insufficient():
    pt, _ = parse_file(FIX / "week2_tooluse_summary.txt")
    assert pt.format == "otter_summary"
    a = assess_source(pt)
    assert a["kind"] == "otter_summary" and not a["sufficient_for_comprehensive_notes"]
    assert [s["heading"] for s in pt.sections] == ["Summary", "Outline", "Action Items"]


def test_docx_and_pdf_extract_images():
    pt, imgs = parse_file(FIX / "week4_rag.docx")
    assert pt.has_timestamps and len(imgs) == 1 and imgs[0]["paragraph_index"] is not None
    pt2, imgs2 = parse_file(FIX / "week4_rag.pdf")
    assert pt2.has_timestamps and len(imgs2) == 1 and imgs2[0]["page"] == 1


def test_pasted_otter_style():
    txt = "Jane Doe  0:05\nHello everyone, welcome.\n\nSpeaker 2  1:02:10\nQuestion about chunking?\n"
    pt = parse_plain(txt)
    assert [s.speaker for s in pt.segments] == ["Jane Doe", "Speaker 2"]
    assert pt.segments[1].start == 3730.0


def test_inline_timestamps_and_vtt_like():
    txt = "[00:00:12] Welcome to class.\n[00:01:30] Today we cover RAG.\n"
    pt = parse_plain(txt)
    assert pt.has_timestamps and len(pt.segments) == 2 and pt.segments[1].start == 90.0
