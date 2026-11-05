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
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import db as dbm
from . import importer
from .config import SECRET_KEYS, load_config, save_config, secret_status, set_secret
from .exporters import export_all, export_docx, export_guide, export_pdf, render_html, render_markdown
from .jobs.runner import runner
from .notes import pipeline  # registers job handlers; also version-history helpers
from .notes.schema import LectureNotes
from .parsers.transcript import format_timestamp
from .providers import ProviderError, get_provider
from .storage import delete_lecture_files, dir_size, lecture_dir, read_drafts, write_drafts
from .config import DATA_DIR

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

def _folder_tree(s) -> list[dict[str, Any]]:
    folders = s.scalars(select(dbm.Folder).order_by(dbm.Folder.order_index, dbm.Folder.name)).all()
    lectures = s.scalars(select(dbm.Lecture).options(selectinload(dbm.Lecture.course)).where(dbm.Lecture.folder_id.isnot(None))
                         .order_by(dbm.Lecture.week, dbm.Lecture.order_index)).all()
    by_parent: dict[str | None, list] = {}
    for f in folders:
        by_parent.setdefault(f.parent_id, []).append(f)
    lec_by_folder: dict[str, list] = {}
    for l in lectures:
        lec_by_folder.setdefault(l.folder_id, []).append(l)

    def build(parent_id, depth=0):
        out = []
        for f in by_parent.get(parent_id, []):
            if depth > 6:
                break
            out.append({"folder": f, "lectures": lec_by_folder.get(f.id, []), "children": build(f.id, depth + 1), "depth": depth})
        return out
    return build(None)


def _folder_choices(s) -> list[tuple[str, str]]:
    """(id, indented path name) for selects."""
    folders = s.scalars(select(dbm.Folder).order_by(dbm.Folder.order_index, dbm.Folder.name)).all()
    by_parent: dict[str | None, list] = {}
    for f in folders:
        by_parent.setdefault(f.parent_id, []).append(f)
    out: list[tuple[str, str]] = []

    def walk(pid, prefix, depth=0):
        for f in by_parent.get(pid, []):
            if depth > 6:
                break
            out.append((f.id, prefix + f.name))
            walk(f.id, prefix + f.name + " / ", depth + 1)
    walk(None, "")
    return out


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with dbm.session() as s:
        courses = s.scalars(select(dbm.Course).options(selectinload(dbm.Course.lectures)).order_by(dbm.Course.name)).all()
        data = []
        for c in courses:
            weeks: dict[Any, list] = {}
            for lec in sorted(c.lectures, key=lambda l: ((l.week or 999), l.lecture_date or "", l.order_index)):
                if lec.folder_id:
                    continue  # shown under its folder instead
                weeks.setdefault(lec.week, []).append(lec)
            if weeks:
                data.append({"course": c, "weeks": sorted(weeks.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))})
        recent = s.scalars(select(dbm.Lecture).where(dbm.Lecture.last_opened_at.isnot(None))
                           .order_by(dbm.Lecture.last_opened_at.desc()).limit(1)).first()
        totals = {"lectures": s.query(dbm.Lecture).count(), "cost": sum(l.cost_usd or 0 for c in courses for l in c.lectures)}
        tree = _folder_tree(s)
        choices = _folder_choices(s)
        return templates.TemplateResponse(request, "dashboard.html", _ctx(request, courses=data, resume=recent, totals=totals,
                                                                            tree=tree, folder_choices=choices))


# ----------------------------------------------------------------- folders

@app.post("/folders")
def folder_create(name: str = Form(...), parent_id: str = Form("")):
    name = name.strip()[:80]
    if not name:
        raise HTTPException(400)
    with dbm.session() as s:
        parent = s.get(dbm.Folder, parent_id) if parent_id else None
        f = dbm.Folder(name=name, parent_id=parent.id if parent else None,
                       order_index=s.query(dbm.Folder).count())
        s.add(f)
        s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/folders/{folder_id}/rename")
def folder_rename(folder_id: str, name: str = Form(...)):
    with dbm.session() as s:
        f = s.get(dbm.Folder, folder_id)
        if not f:
            raise HTTPException(404)
        f.name = name.strip()[:80] or f.name
        s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/folders/{folder_id}/delete")
def folder_delete(folder_id: str):
    """Delete a folder; its lectures and sub-folders move up to the parent (never deleted)."""
    with dbm.session() as s:
        f = s.get(dbm.Folder, folder_id)
        if not f:
            raise HTTPException(404)
        for l in s.scalars(select(dbm.Lecture).where(dbm.Lecture.folder_id == folder_id)).all():
            l.folder_id = f.parent_id
        for child in s.scalars(select(dbm.Folder).where(dbm.Folder.parent_id == folder_id)).all():
            child.parent_id = f.parent_id
        s.delete(f)
        s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/lectures/{lecture_id}/move")
def lecture_move(request: Request, lecture_id: str, folder_id: str = Form("")):
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        lec.folder_id = folder_id if folder_id and s.get(dbm.Folder, folder_id) else None
        s.commit()
    back = request.headers.get("referer") or f"/lectures/{lecture_id}"
    return RedirectResponse(back if back.startswith("http://127.0.0.1") or back.startswith("/") else "/", status_code=303)


# ----------------------------------------------------------------- import

@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    with dbm.session() as s:
        courses = s.scalars(select(dbm.Course).order_by(dbm.Course.name)).all()
        names = [c.name for c in courses]
    cfg = load_config()
    if cfg["default_course"] not in names:
        names.insert(0, cfg["default_course"])
    with dbm.session() as s:
        choices = _folder_choices(s)
    return templates.TemplateResponse(request, "import.html", _ctx(request, course_names=names, folder_choices=choices))


@app.post("/import")
async def import_submit(request: Request, course: str = Form(...), week: str = Form(""), lecture_date: str = Form(""),
                        title: str = Form(""), pasted: str = Form(""), combine: str = Form(""), folder_id: str = Form(""),
                        transcripts: list[UploadFile] = File(default=[]), attachments: list[UploadFile] = File(default=[])):
    week_val = int(week) if week.strip().isdigit() else None
    created: list[str] = []
    errors: list[str] = []
    with dbm.session() as s:
        files = [f for f in transcripts if f.filename]
        if combine and len(files) > 1:
            parts = []
            for up in files:
                data = await up.read()
                if Path(up.filename).suffix.lower() in importer.TRANSCRIPT_EXTS and data:
                    parts.append((up.filename, data))
                else:
                    errors.append(f"{up.filename}: skipped (unsupported type)")
            if parts:
                lec = importer.create_lecture_from_parts(s, course_name=course, title=title.strip() or None, week=week_val,
                                                         lecture_date=lecture_date or None, parts=parts)
                created.append(lec.id)
            files = []
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
        if folder_id and s.get(dbm.Folder, folder_id):
            for lid in created:
                s.get(dbm.Lecture, lid).folder_id = folder_id
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
        anns = [{"id": a.id, "kind": a.kind, "anchor": a.anchor, "anchor_label": a.anchor_label, "body": a.body, "quote": a.quote or "", "mode": a.mode or ""} for a in lec.annotations]
        last_job = sorted(lec.jobs, key=lambda j: j.created_at)[-1] if lec.jobs else None
        images = {im["id"]: im for im in (notes or {}).get("images", [])}
        choices = _folder_choices(s)
        drafts = read_drafts(lecture_id)
        # when editing, prefill from drafts so unpublished work is what you see
        edit_notes = notes
        if view == "edit" and notes and drafts:
            edit_notes = copy.deepcopy(notes)
            for sec, d in drafts.items():
                try:
                    apply_section_edit(edit_notes, sec, d["fields"])
                except HTTPException:
                    continue
        return templates.TemplateResponse(request, "lecture.html", _ctx(
            request, lec=lec, notes=notes, edit_notes=edit_notes, drafts=drafts, transcript=lec.transcript_json or {}, annotations=anns, last_job=last_job,
            view=view, images=images, files=lec.files, assessment=lec.source_assessment or {}, folder_choices=choices))


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
    part = str(form.get("part", "")).strip()
    if part and sections and sections[0].startswith("concept:") and sections[0].count(":") == 1 and part != "whole":
        sections = [f"{sections[0]}:{part}"]
    overwrite = bool(form.get("overwrite_edits"))
    instruction = str(form.get("instruction", ""))[:600]
    if not sections:
        return RedirectResponse(f"/lectures/{lecture_id}?view=edit", status_code=303)
    runner.enqueue(lecture_id, "regenerate_sections", {"sections": sections, "overwrite_edits": overwrite, "instruction": instruction})
    anchor = f"#concept-{sections[0].split(':')[1]}" if sections[0].startswith("concept:") else f"#{sections[0]}"
    return RedirectResponse(f"/lectures/{lecture_id}{anchor}", status_code=303)


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

EDITABLE_CONCEPT_FIELDS = ["title", "in_one_breath", "plain_explanation", "teaching_bullets", "definitions", "why_it_matters", "when_used", "analogy",
                           "analogy_limits", "steps", "connections", "deeper_explanation", "fde_applications"]


def _lines(v: str) -> list[str]:
    return [x.strip() for x in v.replace("\r\n", "\n").split("\n") if x.strip()]


def apply_section_edit(notes: dict[str, Any], section: str, form: dict[str, str]) -> None:
    """Apply edited form fields to the notes model in place."""
    if section == "overview":
        ov = notes["overview"]
        ov["introduction"] = str(form.get("introduction", ov["introduction"]))
        ov["learning_objectives"] = _lines(str(form.get("learning_objectives", "")))
        ov["prerequisites"] = _lines(str(form.get("prerequisites", "")))
        ov["takeaways"] = []
        for line in _lines(str(form.get("takeaways", ""))):
            refs = re.findall(r"⟨([^⟩]+)⟩", line)
            ov["takeaways"].append({"text": re.sub(r"\s*⟨[^⟩]+⟩", "", line).strip(), "provenance": "taught",
                                    "refs": [r.strip() for r in ",".join(refs).split(",") if r.strip()]})
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


def _publish(lecture_id: str, section: str, fields: dict[str, str]) -> None:
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        notes = copy.deepcopy(lec.notes_json or {})  # deep copy: SQLAlchemy only persists a changed value
        if not notes:
            raise HTTPException(400, "No notes to edit")
        apply_section_edit(notes, section, fields)
        LectureNotes.model_validate(notes)  # keep the model consistent
        lec.notes_json = notes
        lec.updated_at = dbm.now()
        dbm.reindex_lecture(s, lec)
        s.commit()
    drafts = read_drafts(lecture_id)
    if section in drafts:
        del drafts[section]
        write_drafts(lecture_id, drafts)


@app.post("/lectures/{lecture_id}/edit/{section}")
async def lecture_edit(request: Request, lecture_id: str, section: str):
    """action=draft saves without changing the notes; action=publish (default) applies the edit."""
    form = await request.form()
    fields = {k: str(v) for k, v in form.items() if k not in ("action",)}
    action = str(form.get("action", "publish"))
    if action == "draft":
        drafts = read_drafts(lecture_id)
        drafts[section] = {"fields": fields, "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        write_drafts(lecture_id, drafts)
        return RedirectResponse(f"/lectures/{lecture_id}?view=edit#edit-{section.replace(':', '-')}", status_code=303)
    if action == "discard":
        drafts = read_drafts(lecture_id)
        drafts.pop(section, None)
        write_drafts(lecture_id, drafts)
        return RedirectResponse(f"/lectures/{lecture_id}?view=edit#edit-{section.replace(':', '-')}", status_code=303)
    _publish(lecture_id, section, fields)
    anchor = f"#concept-{section.split(':', 1)[1]}" if section.startswith("concept:") else f"#{section}"
    return RedirectResponse(f"/lectures/{lecture_id}{anchor}", status_code=303)


@app.post("/api/lectures/{lecture_id}/drafts/{section}")
async def api_draft_save(lecture_id: str, section: str, request: Request):
    """Autosave from the editor (JSON {field: value})."""
    body = await request.json()
    fields = {str(k): str(v) for k, v in dict(body).items() if k != "action"}
    drafts = read_drafts(lecture_id)
    drafts[section] = {"fields": fields, "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    write_drafts(lecture_id, drafts)
    return {"ok": True, "saved_at": drafts[section]["saved_at"]}


@app.delete("/api/lectures/{lecture_id}/drafts/{section}")
def api_draft_discard(lecture_id: str, section: str):
    drafts = read_drafts(lecture_id)
    drafts.pop(section, None)
    write_drafts(lecture_id, drafts)
    return {"ok": True}


@app.post("/lectures/{lecture_id}/drafts/{section}/publish")
def draft_publish(lecture_id: str, section: str):
    drafts = read_drafts(lecture_id)
    d = drafts.get(section)
    if not d:
        raise HTTPException(404, "No draft")
    _publish(lecture_id, section, d["fields"])
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
        return [{"id": a.id, "kind": a.kind, "anchor": a.anchor, "anchor_label": a.anchor_label, "body": a.body, "quote": a.quote or "", "mode": a.mode or ""} for a in anns]


@app.post("/api/lectures/{lecture_id}/explain")
async def api_explain(lecture_id: str, request: Request):
    """Explain / simplify / exemplify a selected passage; saves the result as an 'explanation' annotation."""
    body = await request.json()
    quote = str(body.get("quote", "")).strip()
    anchor = str(body.get("anchor", ""))[:120]
    mode = str(body.get("mode", "explain"))
    if len(quote) < 3:
        raise HTTPException(400, "Select some text first")
    try:
        res = await run_in_threadpool(pipeline.explain_selection, lecture_id, quote, anchor, mode)
    except ProviderError as e:
        raise HTTPException(502, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    with dbm.session() as s:
        a = dbm.Annotation(lecture_id=lecture_id, kind="explanation", anchor=anchor, anchor_label=quote[:200],
                           body=res["explanation"][:8000], quote=quote[:2000], mode=mode)
        s.add(a)
        s.commit()
        return {"ok": True, "id": a.id, "explanation": res["explanation"], "cost_usd": res["cost_usd"], "seconds": res["seconds"], "mode": mode}


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
                           anchor_label=str(body.get("anchor_label", ""))[:200], body=str(body.get("body", ""))[:5000],
                           quote=str(body.get("quote", ""))[:2000])
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


# ----------------------------------------------------------------- re-parse / regenerate-all / versions

@app.post("/lectures/{lecture_id}/reparse")
def lecture_reparse(lecture_id: str):
    with dbm.session() as s:
        _get_lecture(s, lecture_id)
    runner.enqueue(lecture_id, "reparse_and_generate", {})
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


@app.post("/lectures/{lecture_id}/regenerate_all")
def lecture_regenerate_all(lecture_id: str, overwrite_edits: str = Form("")):
    """Regenerate every section with the current prompts while reusing the stored inventory and outline."""
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        if not lec.notes_json:
            raise HTTPException(400, "No notes yet; use Generate.")
        sections = ["overview", "summary", "practice", "glossary", "papers_resources"] + [f"concept:{c['id']}" for c in lec.notes_json["concepts"]]
    runner.enqueue(lecture_id, "regenerate_sections", {"sections": sections, "overwrite_edits": bool(overwrite_edits)})
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


@app.get("/lectures/{lecture_id}/versions", response_class=HTMLResponse)
def lecture_versions(request: Request, lecture_id: str):
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        cur = lec.notes_json or {}
    return templates.TemplateResponse(request, "versions.html", _ctx(request, lec=lec, versions=pipeline.list_versions(lecture_id), current=cur))


@app.get("/lectures/{lecture_id}/versions/{version}", response_class=HTMLResponse)
def lecture_version_view(request: Request, lecture_id: str, version: int, view: str = "notes"):
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        files, assessment, transcript = lec.files, lec.source_assessment or {}, lec.transcript_json or {}
    notes = pipeline.read_version(lecture_id, version)
    if not notes:
        raise HTTPException(404, "Version not found")
    images = {im["id"]: im for im in notes.get("images", [])}
    index = pipeline.read_history_index(lecture_id).get(str(version), {})
    return templates.TemplateResponse(request, "lecture.html", _ctx(
        request, lec=lec, notes=notes, transcript=transcript, annotations=[], last_job=None, view=view if view in ("notes", "interview", "practice", "coverage") else "notes",
        images=images, files=files, assessment=assessment, version_view=version, version_meta=index))


@app.post("/lectures/{lecture_id}/versions/{version}/restore")
def lecture_version_restore(lecture_id: str, version: int):
    if not pipeline.restore_version(lecture_id, version):
        raise HTTPException(404)
    return RedirectResponse(f"/lectures/{lecture_id}", status_code=303)


@app.post("/lectures/{lecture_id}/versions/{version}/star")
def lecture_version_star(lecture_id: str, version: int, label: str = Form("")):
    index = pipeline.read_history_index(lecture_id)
    entry = index.setdefault(str(version), {"version": version})
    if label.strip():
        entry["label"] = label.strip()[:120]
        entry["starred"] = True
    else:
        entry["starred"] = not entry.get("starred", False)
    pipeline.write_history_index(lecture_id, index)
    return RedirectResponse(f"/lectures/{lecture_id}/versions", status_code=303)


@app.post("/lectures/{lecture_id}/versions/snapshot")
def lecture_version_snapshot(lecture_id: str, label: str = Form("")):
    """Save the current notes as a starred version without changing anything."""
    with dbm.session() as s:
        lec = _get_lecture(s, lecture_id)
        if not lec.notes_json:
            raise HTTPException(400)
        v = pipeline.archive_version(lecture_id, lec.notes_json, reason="manual snapshot")
    index = pipeline.read_history_index(lecture_id)
    index[str(v)]["starred"] = True
    index[str(v)]["label"] = label.strip()[:120] or "snapshot"
    pipeline.write_history_index(lecture_id, index)
    return RedirectResponse(f"/lectures/{lecture_id}/versions", status_code=303)


# ----------------------------------------------------------------- study guides

@app.get("/guides", response_class=HTMLResponse)
def guides_page(request: Request):
    with dbm.session() as s:
        guides = s.scalars(select(dbm.StudyGuide).order_by(dbm.StudyGuide.created_at.desc())).all()
        lectures = s.scalars(select(dbm.Lecture).options(selectinload(dbm.Lecture.course)).order_by(dbm.Lecture.week, dbm.Lecture.order_index)).all()
        return templates.TemplateResponse(request, "guides.html", _ctx(request, guides=guides, lectures=lectures))


@app.post("/guides")
async def guides_create(request: Request):
    form = await request.form()
    ids = [str(v) for k, v in form.multi_items() if k == "lecture_ids"]
    title = str(form.get("title", "")).strip()
    with dbm.session() as s:
        lectures = [s.get(dbm.Lecture, i) for i in ids]
        lectures = [l for l in lectures if l and l.notes_json]
        if len(lectures) < 2:
            raise HTTPException(400, "Pick at least two lectures that already have notes.")
        if not title:
            title = "Study guide: " + ", ".join(f"W{l.week or '?'}" for l in lectures)
        g = dbm.StudyGuide(title=title, lecture_ids=[l.id for l in lectures], status="queued")
        s.add(g)
        s.commit()
        gid, first = g.id, lectures[0].id
    runner.enqueue(first, "build_guide", {"guide_id": gid})
    return RedirectResponse(f"/guides/{gid}", status_code=303)


@app.get("/guides/{guide_id}", response_class=HTMLResponse)
def guide_page(request: Request, guide_id: str):
    with dbm.session() as s:
        g = s.get(dbm.StudyGuide, guide_id)
        if not g:
            raise HTTPException(404)
        lectures = {l.id: l for l in [s.get(dbm.Lecture, i) for i in g.lecture_ids] if l}
        job = s.scalars(select(dbm.Job).where(dbm.Job.kind == "build_guide").order_by(dbm.Job.created_at.desc())).all()
        job = next((j for j in job if (j.params or {}).get("guide_id") == guide_id), None)
        return templates.TemplateResponse(request, "guide.html", _ctx(request, g=g, guide=g.guide_json, lectures=lectures, job=job))


@app.post("/guides/{guide_id}/rebuild")
def guide_rebuild(guide_id: str):
    with dbm.session() as s:
        g = s.get(dbm.StudyGuide, guide_id)
        if not g:
            raise HTTPException(404)
        g.status = "queued"
        s.commit()
        first = g.lecture_ids[0]
    runner.enqueue(first, "build_guide", {"guide_id": guide_id})
    return RedirectResponse(f"/guides/{guide_id}", status_code=303)


@app.post("/guides/{guide_id}/delete")
def guide_delete(guide_id: str):
    with dbm.session() as s:
        g = s.get(dbm.StudyGuide, guide_id)
        if g:
            s.delete(g)
            s.commit()
    return RedirectResponse("/guides", status_code=303)


@app.get("/guides/{guide_id}/export/{fmt}")
def guide_export(guide_id: str, fmt: str):
    if fmt not in ("md", "html", "pdf", "docx"):
        raise HTTPException(400)
    with dbm.session() as s:
        g = s.get(dbm.StudyGuide, guide_id)
        if not g or not g.guide_json:
            raise HTTPException(404)
        guide = g.guide_json
    out = export_guide(guide, fmt, DATA_DIR / "guides" / guide_id)
    stem = re.sub(r"[^\w\- ]+", "", guide["title"]).strip().replace(" ", "_")[:60] or "study_guide"
    if fmt == "md":
        return Response(out, media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="{stem}.md"'})
    if fmt == "html":
        return Response(out, media_type="text/html", headers={"Content-Disposition": f'attachment; filename="{stem}.html"'})
    return FileResponse(out, filename=Path(out).name)


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
