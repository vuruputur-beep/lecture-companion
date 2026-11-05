"""Lecture Companion web application (FastAPI, local single-user)."""
from __future__ import annotations

import copy
import json
import logging
import mimetypes
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import db as dbm
from . import importer
from .config import SECRET_KEYS, load_config, save_config, secret_status, set_secret
from .exporters import export_all, export_docx, export_pdf, render_html, render_markdown
from .jobs.runner import runner
from .notes import pipeline  # noqa: F401  (registers job handlers)
from .notes.schema import LectureNotes
from .parsers.transcript import format_timestamp
from .providers import ProviderError, get_provider
from .storage import delete_lecture_files, dir_size, lecture_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("lc.web")
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    dbm.init_db()
    runner.start()
    yield
    runner.stop()


app = FastAPI(title="Lecture Companion", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "web" / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "web" / "templates")
templates.env.filters["ts"] = format_timestamp
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)


def _lecture_dict(lec: dbm.Lecture) -> dict[str, Any]:
    return {"id": lec.id, "title": lec.title, "course": lec.course.name if lec.course else "", "week": lec.week,
            "lecture_date": lec.lecture_date, "status": lec.status, "study_state": lec.study_state}


def _ctx(request: Request, **kw: Any) -> dict[str, Any]:
    cfg = load_config()
    with dbm.session() as s:
        active = s.scalars(select(dbm.Job).where(dbm.Job.status.in_(["queued", "running"]))).all()
        active_jobs = [{"id": j.id, "lecture_id": j.lecture_id, "status": j.status, "progress": j.progress, "message": j.message, "kind": j.kind} for j in active]
    return {"request": request, "cfg": cfg, "active_jobs": active_jobs, **kw}


def _get_lecture(s, lecture_id: str) -> dbm.Lecture:
    lec = s.get(dbm.Lecture, lecture_id, options=[selectinload(dbm.Lecture.files), selectinload(dbm.Lecture.course),
                                                   selectinload(dbm.Lecture.annotations), selectinload(dbm.Lecture.jobs)])
    if not lec:
        raise HTTPException(404, "Lecture not found")
    return lec


# ----------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with dbm.session() as s:
        courses = s.scalars(select(dbm.Course).options(selectinload(dbm.Course.lectures)).order_by(dbm.Course.name)).all()
        data = []
        for c in courses:
            weeks: dict[Any, list] = {}
            for lec in sorted(c.lectures, key=lambda l: ((l.week or 999), l.lecture_date or "", l.order_index)):
                weeks.setdefault(lec.week, []).append(lec)
            data.append({"course": c, "weeks": sorted(weeks.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))})
        recent = s.scalars(select(dbm.Lecture).where(dbm.Lecture.last_opened_at.isnot(None))
                           .order_by(dbm.Lecture.last_opened_at.desc()).limit(1)).first()
        recent_jobs = s.scalars(select(dbm.Job).order_by(dbm.Job.created_at.desc()).limit(8)).all()
        totals = {"lectures": s.query(dbm.Lecture).count(), "cost": sum(l.cost_usd or 0 for c in courses for l in c.lectures)}
        return templates.TemplateResponse(request, "dashboard.html", _ctx(request, courses=data, resume=recent, recent_jobs=recent_jobs, totals=totals))


# ----------------------------------------------------------------- import

@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    with dbm.session() as s:
        courses = s.scalars(select(dbm.Course).order_by(dbm.Course.name)).all()
        names = [c.name for c in courses]
    cfg = load_config()
    if cfg["default_course"] not in names:
        names.insert(0, cfg["default_course"])
    return templates.TemplateResponse(request, "import.html", _ctx(request, course_names=names))


@app.post("/import")
async def import_submit(request: Request, course: str = Form(...), week: str = Form(""), lecture_date: str = Form(""),
                        title: str = Form(""), pasted: str = Form(""),
                        transcripts: list[UploadFile] = File(default=[]), attachments: list[UploadFile] = File(default=[])):
    week_val = int(week) if week.strip().isdigit() else None
    created: list[str] = []
    errors: list[str] = []
    with dbm.session() as s:
        files = [f for f in transcripts if f.filename]
        for i, up in enumerate(files):
            data = await up.read()
            if not data:
                continue
            ext = Path(up.filename).suffix.lower()
            if ext not in importer.TRANSCRIPT_EXTS:
                errors.append(f"{up.filename}: unsupported transcript type {ext}")
                continue
            try:
                lec = importer.create_lecture_from_transcript(
                    s, course_name=course, title=(title if len(files) == 1 and title.strip() else None), week=week_val,
                    lecture_date=lecture_date or None, filename=up.filename, data=data, pasted_text=None)
                created.append(lec.id)
            except Exception as e:  # noqa: BLE001
                log.exception("import failed")
                errors.append(f"{up.filename}: {type(e).__name__}: {e}")
        if pasted.strip():
            lec = importer.create_lecture_from_transcript(
                s, course_name=course, title=(title.strip() or None), week=week_val, lecture_date=lecture_date or None,
                filename=None, data=None, pasted_text=pasted)
            created.append(lec.id)
        # attachments go to the single created lecture, or to the first one when several
        atts = [f for f in attachments if f.filename]
        if atts and created:
            target = s.get(dbm.Lecture, created[0])
            for up in atts:
                data = await up.read()
                if data:
                    importer.attach_file(s, target, up.filename, data)
        s.commit()
    if not created:
        return templates.TemplateResponse(request, "import.html", _ctx(request, course_names=[course], errors=errors or ["Nothing to import: add a file or paste text."]), status_code=400)
    url = "/import/review?ids=" + ",".join(created)
    if errors:
        url += "&errors=" + ";".join(errors)[:500]
    return RedirectResponse(url, status_code=303)


@app.get("/import/review", response_class=HTMLResponse)
def import_review(request: Request, ids: str = "", errors: str = ""):
    id_list = [i for i in ids.split(",") if re.fullmatch(r"[a-f0-9]{12}", i)]
    with dbm.session() as s:
        lectures = [_get_lecture(s, i) for i in id_list]
        previews = []
        for lec in lectures:
            tj = lec.transcript_json or {}
            segs = tj.get("segments", [])
            previews.append({"lec": lec, "segments_head": segs[:6], "segments_tail": segs[-3:] if len(segs) > 9 else [],
                             "assessment": lec.source_assessment or {}, "files": lec.files})
        return templates.TemplateResponse(request, "review.html", _ctx(request, previews=previews, errors=[e for e in errors.split(";") if e]))


@app.post("/import/confirm")
async def import_confirm(request: Request):
    form = await request.form()
    ids = [i for i in str(form.get("ids", "")).split(",") if i]
    to_generate: list[str] = []
    with dbm.session() as s:
        for lid in ids:
            lec = s.get(dbm.Lecture, lid)
            if not lec:
                continue
            if form.get(f"delete_{lid}"):
                delete_lecture_files(lid)
                s.delete(lec)
                continue
            lec.title = str(form.get(f"title_{lid}", lec.title)).strip() or lec.title
            course_name = str(form.get(f"course_{lid}", "")).strip()
            if course_name:
                lec.course_id = importer.get_or_create_course(s, course_name).id
            wk = str(form.get(f"week_{lid}", "")).strip()
            lec.week = int(wk) if wk.isdigit() else None
            lec.lecture_date = str(form.get(f"date_{lid}", "")).strip() or None
            if form.get("generate"):
                to_generate.append(lid)
        s.commit()  # release the write lock before the job runner opens its own session
    for lid in to_generate:
        runner.enqueue(lid, "generate_notes", {})
    return RedirectResponse("/", status_code=303)


# ----------------------------------------------------------------- lecture pages

@app.get("/lectures/{lecture_id}", response_class=HTMLResponse)
def lecture_page(request: Request, lecture_id: str, view: str = "notes"):
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        lec.last_opened_at = dbm.now()
        if lec.study_state == "unread" and lec.notes_json:
            lec.study_state = "read"
        s.commit()
        notes = lec.notes_json
        anns = [{"id": a.id, "kind": a.kind, "anchor": a.anchor, "anchor_label": a.anchor_label, "body": a.body} for a in lec.annotations]
        last_job = sorted(lec.jobs, key=lambda j: j.created_at)[-1] if lec.jobs else None
        images = {im["id"]: im for im in (notes or {}).get("images", [])}
        return templates.TemplateResponse(request, "lecture.html", _ctx(
            request, lec=lec, notes=notes, transcript=lec.transcript_json or {}, annotations=anns, last_job=last_job,
            view=view, images=images, files=lec.files, assessment=lec.source_assessment or {}))


def _ldir(lecture_id: str) -> Path:
    try:
        return lecture_dir(lecture_id)
    except ValueError:
        raise HTTPException(404)


@app.get("/lectures/{lecture_id}/images/{name}")
def lecture_image(lecture_id: str, name: str):
    p = _ldir(lecture_id) / "images" / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/lectures/{lecture_id}/files/{file_id}")
def lecture_file(lecture_id: str, file_id: str):
    with dbm.session() as s:
        f = s.get(dbm.SourceFile, file_id)
        if not f or f.lecture_id != lecture_id:
            raise HTTPException(404)
        p = _ldir(lecture_id) / f.stored_path
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, filename=f.original_name, media_type=f.mime or mimetypes.guess_type(p.name)[0] or "application/octet-stream")


@app.post("/lectures/{lecture_id}/attach")
async def lecture_attach(lecture_id: str, attachments: list[UploadFile] = File(default=[])):
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        for up in attachments:
            if up.filename:
                data = await up.read()
                if data:
                    importer.attach_file(s, lec, up.filename, data)
        s.commit()
    return RedirectResponse(f"/lectures/{lecture_id}?view=source", status_code=303)


@app.post("/lectures/{lecture_id}/generate")
def lecture_generate(lecture_id: str, force: str = Form("")):
    with dbm.session() as s:
        _get_lecture(s, lecture_id)
    runner.enqueue(lecture_id, "generate_notes", {"force": bool(force)})
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


@app.post("/lectures/{lecture_id}/regenerate")
async def lecture_regenerate(request: Request, lecture_id: str):
    form = await request.form()
    sections = [str(v) for k, v in form.multi_items() if k == "sections"]
    overwrite = bool(form.get("overwrite_edits"))
    if not sections:
        return RedirectResponse(f"/lectures/{lecture_id}?view=edit", status_code=303)
    runner.enqueue(lecture_id, "regenerate_sections", {"sections": sections, "overwrite_edits": overwrite})
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


@app.post("/lectures/{lecture_id}/state")
def lecture_state(lecture_id: str, state: str = Form(...)):
    if state not in ("unread", "read", "practiced", "reviewed"):
        raise HTTPException(400)
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        lec.study_state = state
        s.commit()
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


@app.post("/lectures/{lecture_id}/delete")
def lecture_delete(lecture_id: str, what: str = Form("all")):
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        if what == "originals":
            delete_lecture_files(lecture_id, originals=True, generated=False)
            for f in list(lec.files):
                if f.kind in ("transcript", "pasted", "slides", "recording", "other"):
                    s.delete(f)
            lec.transcript_json = None if not lec.notes_json else lec.transcript_json  # keep citations usable when notes exist
        elif what == "generated":
            delete_lecture_files(lecture_id, originals=False, generated=True)
            lec.notes_json, lec.coverage_json, lec.notes_version = None, None, 0
            lec.status = "imported"
            s.execute(dbm.text("DELETE FROM search_index WHERE lecture_id = :lid AND kind != 'transcript'"), {"lid": lecture_id})
        else:
            delete_lecture_files(lecture_id)
            s.execute(dbm.text("DELETE FROM search_index WHERE lecture_id = :lid"), {"lid": lecture_id})
            s.delete(lec)
            s.commit()
            return RedirectResponse("/", status_code=303)
        s.commit()
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


# ----------------------------------------------------------------- editing

EDITABLE_CONCEPT_FIELDS = ["title", "plain_explanation", "teaching_bullets", "definitions", "why_it_matters", "when_used", "analogy",
                           "analogy_limits", "steps", "connections", "deeper_explanation", "fde_applications"]


def _lines(v: str) -> list[str]:
    return [x.strip() for x in v.replace("\r\n", "\n").split("\n") if x.strip()]


@app.post("/lectures/{lecture_id}/edit/{section}")
async def lecture_edit(request: Request, lecture_id: str, section: str):
    form = await request.form()
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        notes = copy.deepcopy(lec.notes_json or {})  # deep copy: SQLAlchemy only persists a changed value
        if not notes:
            raise HTTPException(400, "No notes to edit")
        if section == "overview":
            ov = notes["overview"]
            ov["introduction"] = str(form.get("introduction", ov["introduction"]))
            ov["learning_objectives"] = _lines(str(form.get("learning_objectives", "")))
            ov["prerequisites"] = _lines(str(form.get("prerequisites", "")))
            ov["takeaways"] = [{"text": t, "provenance": "taught", "refs": []} if not isinstance(t, dict) else t for t in _lines(str(form.get("takeaways", "")))]
            ov["user_edited_fields"] = ["introduction", "learning_objectives", "prerequisites", "takeaways"]
        elif section == "summary":
            sm = notes["summary"]
            sm["main_lessons"] = _lines(str(form.get("main_lessons", "")))
            sm["how_they_connect"] = str(form.get("how_they_connect", ""))
            sm["suggested_next_steps"] = _lines(str(form.get("suggested_next_steps", "")))
            sm["user_edited_fields"] = ["main_lessons", "how_they_connect", "suggested_next_steps"]
        elif section.startswith("concept:"):
            cid = section.split(":", 1)[1]
            c = next((c for c in notes["concepts"] if c["id"] == cid), None)
            if not c:
                raise HTTPException(404)
            edited = set(c.get("user_edited_fields", []))
            for f in EDITABLE_CONCEPT_FIELDS:
                if f not in form:
                    continue
                v = str(form.get(f))
                if f in ("teaching_bullets", "fde_applications"):
                    new = []
                    for line in _lines(v):
                        prov = "taught"
                        m = re.match(r"^\[(T|A|\?)\]\s*(.*)$", line)
                        if m:
                            prov = {"T": "taught", "A": "additional", "?": "needs_verification"}[m.group(1)]
                            line = m.group(2)
                        refs = re.findall(r"⟨([^⟩]+)⟩", line)
                        line = re.sub(r"\s*⟨[^⟩]+⟩", "", line).strip()
                        new.append({"text": line, "provenance": prov, "refs": [r.strip() for r in ",".join(refs).split(",") if r.strip()]})
                    c[f] = new
                elif f == "definitions":
                    new = []
                    for line in _lines(v):
                        term, _, definition = line.partition(" - ")
                        new.append({"term": term.strip().strip("*"), "definition": definition.strip(), "provenance": "taught", "refs": []})
                    c[f] = new
                elif f == "steps":
                    c[f] = [re.sub(r"^\d+[.)]\s*", "", x) for x in _lines(v)]
                else:
                    c[f] = v.strip()
                edited.add(f)
            c["user_edited_fields"] = sorted(edited)
        else:
            raise HTTPException(400, "Unknown section")
        LectureNotes.model_validate(notes)  # keep the model consistent
        lec.notes_json = notes
        lec.updated_at = dbm.now()
        dbm.reindex_lecture(s, lec)
        s.commit()
    anchor = f"#concept-{section.split(':', 1)[1]}" if section.startswith("concept:") else f"#{section}"
    return RedirectResponse(f"/lectures/{lecture_id}{anchor}", status_code=303)


# ----------------------------------------------------------------- reading position / annotations

@app.post("/api/lectures/{lecture_id}/position")
async def api_position(lecture_id: str, request: Request):
    body = await request.json()
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lecture_id)
        if lec:
            lec.reading_position = str(body.get("anchor", ""))[:120]
            lec.reading_progress = float(body.get("progress", 0) or 0)
            s.commit()
    return {"ok": True}


@app.get("/api/lectures/{lecture_id}/annotations")
def api_annotations(lecture_id: str):
    with dbm.session() as s:
        anns = s.scalars(select(dbm.Annotation).where(dbm.Annotation.lecture_id == lecture_id)).all()
        return [{"id": a.id, "kind": a.kind, "anchor": a.anchor, "anchor_label": a.anchor_label, "body": a.body} for a in anns]


@app.post("/api/lectures/{lecture_id}/annotations")
async def api_annotation_create(lecture_id: str, request: Request):
    body = await request.json()
    kind = body.get("kind")
    if kind not in ("bookmark", "note", "review", "practiced"):
        raise HTTPException(400)
    with dbm.session() as s:
        if kind in ("bookmark", "review", "practiced"):
            existing = s.scalars(select(dbm.Annotation).where(dbm.Annotation.lecture_id == lecture_id, dbm.Annotation.kind == kind,
                                                               dbm.Annotation.anchor == body.get("anchor", ""))).first()
            if existing:  # toggle off
                s.delete(existing)
                s.commit()
                return {"ok": True, "removed": True}
        a = dbm.Annotation(lecture_id=lecture_id, kind=kind, anchor=str(body.get("anchor", ""))[:120],
                           anchor_label=str(body.get("anchor_label", ""))[:200], body=str(body.get("body", ""))[:5000])
        s.add(a)
        lec = s.get(dbm.Lecture, lecture_id)
        if lec and kind == "practiced" and lec.study_state in ("unread", "read"):
            lec.study_state = "practiced"
        if lec and kind == "review" and lec.study_state != "reviewed":
            pass
        s.commit()
        return {"ok": True, "id": a.id}


@app.delete("/api/annotations/{ann_id}")
def api_annotation_delete(ann_id: str):
    with dbm.session() as s:
        a = s.get(dbm.Annotation, ann_id)
        if a:
            s.delete(a)
            s.commit()
    return {"ok": True}


# ----------------------------------------------------------------- search / interview / practice

@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", lecture: str = ""):
    results = []
    with dbm.session() as s:
        if q.strip():
            results = dbm.search(s, q, lecture or None)
            titles = {l.id: l.title for l in s.scalars(select(dbm.Lecture)).all()}
            for r in results:
                r["lecture_title"] = titles.get(r["lecture_id"], "?")
        lectures = s.scalars(select(dbm.Lecture).order_by(dbm.Lecture.week)).all()
        return templates.TemplateResponse(request, "search.html", _ctx(request, q=q, results=results, lectures=lectures, lecture_filter=lecture))


@app.get("/interview", response_class=HTMLResponse)
def interview_page(request: Request, lecture: str = ""):
    with dbm.session() as s:
        q = select(dbm.Lecture).options(selectinload(dbm.Lecture.course)).where(dbm.Lecture.notes_json.isnot(None)).order_by(dbm.Lecture.week)
        lectures = s.scalars(q).all()
        if lecture:
            lectures = [l for l in lectures if l.id == lecture]
        cards = []
        for lec in lectures:
            for c in (lec.notes_json or {}).get("concepts", []):
                for i, iq in enumerate(c.get("interview_questions", [])):
                    cards.append({"lecture": lec, "concept": c, "q": iq, "anchor": f"concept-{c['id']}-iq{i}"})
        all_lectures = s.scalars(select(dbm.Lecture).order_by(dbm.Lecture.week)).all()
        return templates.TemplateResponse(request, "interview.html", _ctx(request, cards=cards, lectures=all_lectures, lecture_filter=lecture))


# ----------------------------------------------------------------- exports

@app.get("/lectures/{lecture_id}/export/{fmt}")
def lecture_export(lecture_id: str, fmt: str, mode: str = "detailed"):
    if fmt not in ("md", "html", "pdf", "docx") or mode not in ("detailed", "concise"):
        raise HTTPException(400)
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        if not lec.notes_json:
            raise HTTPException(400, "No notes yet")
        ld = _lecture_dict(lec)
        notes, transcript = lec.notes_json, lec.transcript_json
    out_dir = lecture_dir(lecture_id) / "exports"
    stem = re.sub(r"[^\w\- ]+", "", ld["title"]).strip().replace(" ", "_")[:60] or "lecture"
    stem = f"{stem}_{mode}"
    if fmt == "md":
        return Response(render_markdown(ld, notes, mode, transcript), media_type="text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.md"'})
    if fmt == "html":
        return Response(render_html(ld, notes, mode, transcript), media_type="text/html",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.html"'})
    path = out_dir / f"{stem}.{fmt}"
    try:
        if fmt == "pdf":
            export_pdf(ld, notes, mode, transcript, path)
        else:
            export_docx(ld, notes, mode, transcript, path)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return FileResponse(path, filename=path.name)


# ----------------------------------------------------------------- jobs

@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    with dbm.session() as s:
        jobs = s.scalars(select(dbm.Job).options(selectinload(dbm.Job.lecture)).order_by(dbm.Job.created_at.desc()).limit(100)).all()
        calls = s.scalars(select(dbm.ApiCallLog).order_by(dbm.ApiCallLog.created_at.desc()).limit(200)).all()
        agg = {"calls": len(calls), "cost": sum(c.cost_usd for c in calls), "tin": sum(c.tokens_in for c in calls), "tout": sum(c.tokens_out for c in calls),
               "seconds": sum(c.seconds for c in calls)}
        return templates.TemplateResponse(request, "jobs.html", _ctx(request, jobs=jobs, calls=calls[:50], agg=agg))


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    with dbm.session() as s:
        j = s.get(dbm.Job, job_id)
        if not j:
            raise HTTPException(404)
        return {"id": j.id, "status": j.status, "progress": j.progress, "message": j.message, "error": j.error,
                "cost_usd": j.cost_usd, "tokens_in": j.tokens_in, "tokens_out": j.tokens_out, "log": (j.log or [])[-15:],
                "lecture_id": j.lecture_id}


@app.get("/api/lectures/{lecture_id}/jobs")
def api_lecture_jobs(lecture_id: str):
    with dbm.session() as s:
        jobs = s.scalars(select(dbm.Job).where(dbm.Job.lecture_id == lecture_id).order_by(dbm.Job.created_at.desc()).limit(5)).all()
        lec = s.get(dbm.Lecture, lecture_id)
        return {"lecture_status": lec.status if lec else None,
                "jobs": [{"id": j.id, "status": j.status, "progress": j.progress, "message": j.message, "error": j.error, "kind": j.kind,
                          "cost_usd": j.cost_usd} for j in jobs]}


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    runner.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def api_job_retry(job_id: str):
    runner.retry(job_id)
    return {"ok": True}


# ----------------------------------------------------------------- settings

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    from .exporters.render import find_chrome
    return templates.TemplateResponse(request, "settings.html", _ctx(request, secrets=secret_status(), secret_keys=SECRET_KEYS, msg=msg,
                                                            chrome=find_chrome(), data_size=dir_size(lecture_dir.__globals__["LECTURES_DIR"])))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    updates = {k: v for k, v in form.items() if k in load_config()}
    if "verify_links" not in form:
        updates["verify_links"] = False
    save_config(updates)
    return RedirectResponse("/settings?msg=Settings+saved", status_code=303)


@app.post("/settings/secret")
def settings_secret(request: Request, name: str = Form(...), value: str = Form("")):
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Secrets can only be set from localhost")
    if name not in SECRET_KEYS:
        raise HTTPException(400)
    set_secret(name, value or None)
    return RedirectResponse("/settings?msg=" + ("Key+saved+to+data/secrets.env" if value else "Key+removed"), status_code=303)


@app.post("/settings/test")
def settings_test():
    try:
        res = get_provider().check()
    except ProviderError as e:
        res = {"ok": False, "detail": str(e)}
    msg = ("OK: " if res["ok"] else "FAILED: ") + res["detail"]
    return RedirectResponse("/settings?msg=" + msg.replace(" ", "+")[:300], status_code=303)


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}
