"""Versions, combined imports, study guides (mock provider)."""
import os
from pathlib import Path

import pytest

os.environ["LC_DATA_DIR"] = str(Path(__file__).parent / "_data_test2")
os.environ["LC_PROVIDER"] = "mock"
os.environ["LC_VERIFY_LINKS"] = "false"

from app import db as dbm  # noqa: E402
from app import importer  # noqa: E402
from app.jobs.runner import JobContext, _handlers  # noqa: E402
from app.notes import pipeline  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def _run(lid, kind, params):
    with dbm.session() as s:
        job = dbm.Job(lecture_id=lid, kind=kind, params=params, status="running")
        s.add(job)
        s.commit()
        jid = job.id
    _handlers[kind](JobContext(jid, lid), params)


@pytest.fixture(scope="module")
def two_lectures():
    import shutil
    shutil.rmtree(os.environ["LC_DATA_DIR"], ignore_errors=True)
    dbm._engine = None
    dbm.init_db()
    ids = []
    for name in ("week4_rag_otter.txt", "week3_eval_untimed.txt"):
        with dbm.session() as s:
            lec = importer.create_lecture_from_transcript(s, course_name="C", title=None, week=None, lecture_date=None,
                                                          filename=name, data=(FIX / name).read_bytes(), pasted_text=None)
            s.commit()
            ids.append(lec.id)
        _run(ids[-1], "generate_notes", {})
    return ids


def test_combined_import_keeps_parts_and_order(two_lectures):
    with dbm.session() as s:
        lec = importer.create_lecture_from_parts(s, course_name="C", title="Combined", week=5, lecture_date=None,
                                                 parts=[("part1.txt", (FIX / "week3_eval_untimed.txt").read_bytes()),
                                                        ("part2.srt", (FIX / "week4_rag.srt").read_bytes())])
        s.commit()
        tj = lec.transcript_json
        notes = lec.source_assessment["notes"]
    assert len(tj["parts"]) == 2
    segs = tj["segments"]
    assert segs[0]["part"] == 1 and segs[-1]["part"] == 2
    assert segs[0]["pid"] == "P1" and segs[-1]["pid"] == f"P{len(segs)}"
    assert tj["duration_seconds"] is None  # no combined clock is claimed
    assert any("Combined from 2 files" in n for n in notes)


def test_versions_archive_star_restore(two_lectures):
    lid = two_lectures[0]
    with dbm.session() as s:
        v1 = s.get(dbm.Lecture, lid).notes_json["version"]
    _run(lid, "regenerate_sections", {"sections": ["summary"]})
    versions = pipeline.list_versions(lid)
    assert [v["version"] for v in versions] == [v1]
    assert pipeline.read_version(lid, v1)["version"] == v1
    idx = pipeline.read_history_index(lid)
    idx[str(v1)]["starred"], idx[str(v1)]["label"] = True, "liked"
    pipeline.write_history_index(lid, idx)
    assert pipeline.list_versions(lid)[0]["label"] == "liked"
    assert pipeline.restore_version(lid, v1)
    with dbm.session() as s:
        n = s.get(dbm.Lecture, lid).notes_json
    assert n["restored_from"] == v1 and n["version"] == v1 + 2
    assert len(pipeline.list_versions(lid)) == 2  # the replaced v2 was archived too


def test_build_guide(two_lectures):
    with dbm.session() as s:
        g = dbm.StudyGuide(title="Guide", lecture_ids=two_lectures, status="queued")
        s.add(g)
        s.commit()
        gid = g.id
    _run(two_lectures[0], "build_guide", {"guide_id": gid})
    with dbm.session() as s:
        g = s.get(dbm.StudyGuide, gid)
        assert g.status == "ready"
        guide = g.guide_json
    assert guide["themes"] and guide["learning_path"]
    q = guide["interview_bank"][0]["questions"][0]
    assert q["question"] and q["short_answer"]  # resolved from the lecture's real questions
    assert len(guide["lectures"]) == 2
    from app.exporters import export_guide
    md = export_guide(guide, "md", Path(os.environ["LC_DATA_DIR"]))
    assert "Interview question bank" in md
    docx_path = export_guide(guide, "docx", Path(os.environ["LC_DATA_DIR"]) / "g")
    assert Path(docx_path).stat().st_size > 3000


def test_reparse_job(two_lectures):
    lid = two_lectures[1]
    _run(lid, "reparse_and_generate", {})
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lid)
        assert lec.notes_json["version"] >= 2 and lec.transcript_json["segments"]


def test_regenerate_single_concept_part_keeps_rest(two_lectures):
    lid = two_lectures[0]
    with dbm.session() as s:
        n = s.get(dbm.Lecture, lid).notes_json
        c = n["concepts"][0]
        cid, old_plain, old_q = c["id"], c["plain_explanation"], c["interview_questions"][0]["question"]
    _run(lid, "regenerate_sections", {"sections": [f"concept:{cid}:interview_questions"], "instruction": "harder questions"})
    with dbm.session() as s:
        c2 = s.get(dbm.Lecture, lid).notes_json["concepts"][0]
    assert c2["plain_explanation"] == old_plain  # untouched part preserved
    assert c2["interview_questions"][0]["question"] != old_q  # requested part regenerated


def test_explain_selection_api(two_lectures):
    from fastapi.testclient import TestClient
    from app.main import app
    lid = two_lectures[0]
    with TestClient(app) as c:
        with dbm.session() as s:
            cid = s.get(dbm.Lecture, lid).notes_json["concepts"][0]["id"]
        r = c.post(f"/api/lectures/{lid}/explain", json={"quote": "RAG lets a model look things up", "anchor": f"concept-{cid}", "mode": "simpler"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["explanation"] and d["mode"] == "simpler"
        r = c.post(f"/api/lectures/{lid}/annotations", json={"kind": "note", "anchor": f"concept-{cid}", "body": "my thought", "quote": "look things up"})
        assert r.status_code == 200
        anns = c.get(f"/api/lectures/{lid}/annotations").json()
        kinds = {(a["kind"], a["quote"]) for a in anns}
        assert ("explanation", "RAG lets a model look things up") in kinds and ("note", "look things up") in kinds
        assert c.post(f"/api/lectures/{lid}/explain", json={"quote": "x", "anchor": ""}).status_code == 400
        page = c.get(f"/lectures/{lid}").text
        assert 'id="selToolbar"' in page and '"quote": "look things up"' in page


def test_folders_create_move_nest_delete(two_lectures):
    from fastapi.testclient import TestClient
    from app.main import app
    import uuid
    lid = two_lectures[0]
    top, subname = f"Module {uuid.uuid4().hex[:6]}", f"Week {uuid.uuid4().hex[:6]}"
    with TestClient(app) as c:
        assert c.post("/folders", data={"name": top}, follow_redirects=False).status_code == 303
        with dbm.session() as s:
            fid = s.query(dbm.Folder).filter(dbm.Folder.name == top).one().id
        assert c.post("/folders", data={"name": subname, "parent_id": fid}, follow_redirects=False).status_code == 303
        with dbm.session() as s:
            sub = s.query(dbm.Folder).filter(dbm.Folder.name == subname).one()
            assert sub.parent_id == fid
            sub_id = sub.id
        assert c.post(f"/lectures/{lid}/move", data={"folder_id": sub_id}, follow_redirects=False).status_code == 303
        home = c.get("/").text
        assert f"📁 {top}" in home and f"📁 {subname}" in home
        folder_body = home[home.index(f"📁 {subname}"):]
        assert 'class="card lecture-card' in folder_body and f'href="/lectures/{lid}"' in folder_body  # card shown under its folder
        assert "Recent jobs" not in home
        assert c.post(f"/folders/{sub_id}/delete", follow_redirects=False).status_code == 303
        with dbm.session() as s:
            assert s.get(dbm.Lecture, lid).folder_id == fid  # moved up to the parent, not lost
        assert c.post(f"/folders/{fid}/rename", data={"name": top + " renamed"}, follow_redirects=False).status_code == 303
        assert top + " renamed" in c.get("/").text
        assert c.get(f"/lectures/{lid}").status_code == 200
        assert 'id="importForm"' in c.get("/import").text


def test_drafts_do_not_change_notes_until_published(two_lectures):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.storage import read_drafts
    lid = two_lectures[1]
    with TestClient(app) as c:
        with dbm.session() as s:
            before = s.get(dbm.Lecture, lid).notes_json["overview"]["introduction"]
        # autosave from the editor
        r = c.post(f"/api/lectures/{lid}/drafts/overview", json={"introduction": "DRAFT INTRO", "learning_objectives": "a\nb", "prerequisites": "", "takeaways": "t1 ⟨P1⟩"})
        assert r.status_code == 200 and read_drafts(lid)["overview"]["fields"]["introduction"] == "DRAFT INTRO"
        with dbm.session() as s:
            assert s.get(dbm.Lecture, lid).notes_json["overview"]["introduction"] == before  # unchanged
        # editor shows the draft, notes view flags it
        edit = c.get(f"/lectures/{lid}?view=edit").text
        assert "DRAFT INTRO" in edit and "unpublished" in edit
        assert "draft pending" in c.get(f"/lectures/{lid}").text
        # explicit save-draft via the form keeps it a draft
        r = c.post(f"/lectures/{lid}/edit/overview", data={"action": "draft", "introduction": "DRAFT 2", "learning_objectives": "", "prerequisites": "", "takeaways": "t1"}, follow_redirects=False)
        assert r.status_code == 303
        with dbm.session() as s:
            assert s.get(dbm.Lecture, lid).notes_json["overview"]["introduction"] == before
        # publish the draft
        r = c.post(f"/lectures/{lid}/drafts/overview/publish", follow_redirects=False)
        assert r.status_code == 303
        with dbm.session() as s:
            ov = s.get(dbm.Lecture, lid).notes_json["overview"]
            assert ov["introduction"] == "DRAFT 2" and "introduction" in ov["user_edited_fields"]
        assert "overview" not in read_drafts(lid)
        # discard path
        c.post(f"/api/lectures/{lid}/drafts/summary", json={"main_lessons": "x", "how_they_connect": "y", "suggested_next_steps": ""})
        assert c.delete(f"/api/lectures/{lid}/drafts/summary").status_code == 200 and "summary" not in read_drafts(lid)
        # folder picker is not inside the card link any more
        home = c.get("/").text
        assert 'class="cardlink"' in home and "<a" not in home[home.index('class="movef"'):home.index('class="movef"') + 40]
