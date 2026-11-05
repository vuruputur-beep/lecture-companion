"""End-to-end: import fixtures → generate notes with the mock provider → edit → regenerate → export."""
import json
import os
from pathlib import Path

import pytest

os.environ["LC_DATA_DIR"] = str(Path(__file__).parent / "_data_test")
os.environ["LC_PROVIDER"] = "mock"
os.environ["LC_CHUNK_WORDS"] = "3000"
os.environ["LC_VERIFY_LINKS"] = "false"

from app import db as dbm  # noqa: E402
from app import importer  # noqa: E402
from app.config import load_config  # noqa: E402
from app.exporters import export_all  # noqa: E402
from app.jobs.runner import JobContext  # noqa: E402
from app.notes import pipeline  # noqa: E402
from app.notes.schema import LectureNotes  # noqa: E402
from app.storage import lecture_dir  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def lecture_id():
    import shutil
    shutil.rmtree(os.environ["LC_DATA_DIR"], ignore_errors=True)
    dbm._engine = None
    dbm.init_db()
    with dbm.session() as s:
        lec = importer.create_lecture_from_transcript(
            s, course_name="Test course", title=None, week=None, lecture_date=None,
            filename="week4_rag_otter.txt", data=(FIX / "week4_rag_otter.txt").read_bytes(), pasted_text=None)
        importer.attach_file(s, lec, "screenshot_fixture.png", (FIX / "screenshot_fixture.png").read_bytes())
        importer.attach_file(s, lec, "week4_slides.pdf", (FIX / "week4_slides.pdf").read_bytes())
        s.commit()
        return lec.id


def _job(lecture_id, kind, params):
    with dbm.session() as s:
        job = dbm.Job(lecture_id=lecture_id, kind=kind, params=params, status="running")
        s.add(job)
        s.commit()
        jid = job.id
    ctx = JobContext(jid, lecture_id)
    return ctx


def test_import_metadata(lecture_id):
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lecture_id)
        assert lec.week == 4 and lec.title.startswith("FDE Week 4")
        assert lec.lecture_date == "2025-10-26"
        assert lec.source_assessment["kind"] == "full_transcript"
        kinds = {f.kind for f in lec.files}
        assert {"transcript", "screenshot", "slides", "slides_page"} <= kinds


def test_generate_notes_with_mock(lecture_id):
    assert load_config()["provider"] == "mock"
    ctx = _job(lecture_id, "generate_notes", {})
    pipeline.generate_notes(ctx, {})
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lecture_id)
        notes = LectureNotes.model_validate(lec.notes_json)
    assert len(notes.concepts) >= 3
    assert all(len(c.teaching_bullets) >= 10 for c in notes.concepts)
    assert len(notes.overview.takeaways) >= 10
    assert notes.coverage.segments_total > 100 and notes.coverage.chunk_count >= 2
    assert notes.coverage.beginning_covered and notes.coverage.end_covered
    assert notes.images and notes.images[0].timestamp is None  # screenshots carry no invented timestamps
    assert (lecture_dir(lecture_id) / "pipeline" / "outline.json").exists()


def test_checkpoint_reuse_skips_finished_steps(lecture_id):
    """A second run must not redo chunks/concepts: the mock provider counts calls."""
    from app.providers.mock import MockProvider
    from app.providers import base
    counter = MockProvider()
    orig = base.get_provider
    base.get_provider = lambda cfg=None: counter
    pipeline.get_provider = lambda cfg=None: counter
    try:
        ctx = _job(lecture_id, "generate_notes", {})
        pipeline.generate_notes(ctx, {})
    finally:
        base.get_provider = orig
        pipeline.get_provider = orig
    assert counter.calls == 0


def test_edit_then_regenerate_preserves_edits(lecture_id):
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lecture_id)
        import copy
        notes = copy.deepcopy(lec.notes_json)
        cid = notes["concepts"][0]["id"]
        notes["concepts"][0]["plain_explanation"] = "MY EDIT"
        notes["concepts"][0]["user_edited_fields"] = ["plain_explanation"]
        notes["overview"]["introduction"] = "MY INTRO"
        notes["overview"]["user_edited_fields"] = ["introduction"]
        lec.notes_json = notes
        s.commit()
    ctx = _job(lecture_id, "regenerate_sections", {})
    pipeline.regenerate_sections(ctx, {"sections": [f"concept:{cid}", "overview", "summary"], "overwrite_edits": False})
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lecture_id)
        n = lec.notes_json
        assert n["concepts"][0]["plain_explanation"] == "MY EDIT"
        assert n["overview"]["introduction"] == "MY INTRO"
        assert n["version"] >= 3
    ctx = _job(lecture_id, "regenerate_sections", {})
    pipeline.regenerate_sections(ctx, {"sections": [f"concept:{cid}"], "overwrite_edits": True})
    with dbm.session() as s:
        n = s.get(dbm.Lecture, lecture_id).notes_json
        assert n["concepts"][0]["plain_explanation"] != "MY EDIT"
        assert n["overview"]["introduction"] == "MY INTRO"  # untouched section keeps its edit


def test_exports_all_formats(lecture_id):
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lecture_id)
        ld = {"id": lec.id, "title": lec.title, "course": "Test course", "week": lec.week, "lecture_date": lec.lecture_date}
        notes, tj = lec.notes_json, lec.transcript_json
    out = export_all(ld, notes, tj, lecture_dir(lecture_id) / "exports", "detailed")
    assert out["md"].stat().st_size > 10_000
    assert out["docx"].stat().st_size > 5_000
    html = out["html"].read_text()
    assert "data:image/png;base64" in html  # images embedded for offline use
    assert "Concept 1" in html and "Coverage report" in html
    if "pdf" in out:
        assert out["pdf"].stat().st_size > 20_000
    concise = export_all(ld, notes, tj, lecture_dir(lecture_id) / "exports", "concise")
    assert concise["md"].stat().st_size < out["md"].stat().st_size


def test_summary_source_produces_insufficiency_note():
    with dbm.session() as s:
        lec = importer.create_lecture_from_transcript(
            s, course_name="Test course", title=None, week=None, lecture_date=None,
            filename="week2_tooluse_summary.txt", data=(FIX / "week2_tooluse_summary.txt").read_bytes(), pasted_text=None)
        s.commit()
        lid = lec.id
    ctx = _job(lid, "generate_notes", {})
    pipeline.generate_notes(ctx, {})
    with dbm.session() as s:
        n = s.get(dbm.Lecture, lid).notes_json
        assert n["insufficiency_note"]
        assert n["source_kind"] == "otter_summary"


def test_search_index(lecture_id):
    with dbm.session() as s:
        res = dbm.search(s, "retrieval augmented")
        assert res and any(r["kind"] == "transcript" for r in res)


def test_coverage_detects_unreferenced_stretch():
    segs = [{"idx": i, "pid": f"P{i+1}", "text": "word " * 60, "start": None} for i in range(30)]
    concepts = [{"refs": ["P1", "P10"], "teaching_bullets": [], "definitions": [], "examples": [], "moments": [],
                 "fde_applications": [], "brief_mentions": [], "pitfalls": {}}]
    cov = pipeline.compute_coverage(segs, concepts, [], 1)
    assert cov["beginning_covered"] and not cov["end_covered"]
    assert cov["gaps"] and cov["gaps"][0]["refs"] == ["P11", "P30"]
