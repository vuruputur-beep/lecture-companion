"""SQLite persistence via SQLAlchemy 2.0 (sync).

One lecture = one row + one isolated directory under data/lectures/<id>/.
Notes are stored as JSON documents (the single structured model used by the
UI and every export format). Full-text search uses SQLite FTS5.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy import text as text  # re-exported for routes
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import DB_PATH, ensure_dirs


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Base(DeclarativeBase):
    pass


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    lectures: Mapped[list["Lecture"]] = relationship(back_populates="course")


class Lecture(Base):
    __tablename__ = "lectures"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"))
    title: Mapped[str] = mapped_column(String)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lecture_date: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO date
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="imported")
    # imported | queued | processing | processed | failed
    study_state: Mapped[str] = mapped_column(String, default="unread")  # unread|read|practiced|reviewed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    # Canonical transcript (parsed) - list of segments; see parsers.transcript
    transcript_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_kind: Mapped[str] = mapped_column(String, default="unknown")
    # full_transcript | otter_summary | mixed | insufficient
    source_assessment: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    notes_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notes_version: Mapped[int] = mapped_column(Integer, default=0)
    coverage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    processing_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    reading_position: Mapped[str | None] = mapped_column(String, nullable=True)
    reading_progress: Mapped[float] = mapped_column(Float, default=0.0)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    course: Mapped[Course] = relationship(back_populates="lectures")
    files: Mapped[list["SourceFile"]] = relationship(back_populates="lecture", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="lecture", cascade="all, delete-orphan")
    annotations: Mapped[list["Annotation"]] = relationship(back_populates="lecture", cascade="all, delete-orphan")


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    lecture_id: Mapped[str] = mapped_column(ForeignKey("lectures.id"))
    kind: Mapped[str] = mapped_column(String)  # transcript | slides | screenshot | recording | image | pasted
    original_name: Mapped[str] = mapped_column(String)
    stored_path: Mapped[str] = mapped_column(String)  # relative to lecture dir
    mime: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    lecture: Mapped[Lecture] = relationship(back_populates="files")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    lecture_id: Mapped[str] = mapped_column(ForeignKey("lectures.id"))
    kind: Mapped[str] = mapped_column(String)  # generate_notes | regenerate_sections | export
    status: Mapped[str] = mapped_column(String, default="queued")
    # queued | running | succeeded | failed | cancelled
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    checkpoint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    log: Mapped[list | None] = mapped_column(JSON, nullable=True)
    lecture: Mapped[Lecture] = relationship(back_populates="jobs")


class Annotation(Base):
    """Bookmarks, highlights/notes, and review markers anchored to element ids."""
    __tablename__ = "annotations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    lecture_id: Mapped[str] = mapped_column(ForeignKey("lectures.id"))
    kind: Mapped[str] = mapped_column(String)  # bookmark | note | review | practiced
    anchor: Mapped[str] = mapped_column(String)  # element id within the lecture page
    anchor_label: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    lecture: Mapped[Lecture] = relationship(back_populates="annotations")


class ApiCallLog(Base):
    """Every model call, for measured cost/time reporting."""
    __tablename__ = "api_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lecture_id: Mapped[str | None] = mapped_column(String, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    step: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cache_read: Mapped[int] = mapped_column(Integer, default=0)
    cache_write: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


_engine = None
SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    global _engine, SessionLocal
    if _engine is None:
        ensure_dirs()
        _engine = create_engine(
            f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(_engine, "connect")
        def _set_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5("
            "lecture_id UNINDEXED, kind UNINDEXED, anchor UNINDEXED, title, body, tokenize='porter unicode61')"
        ))


def session() -> Session:
    get_engine()
    assert SessionLocal is not None
    return SessionLocal()


# ---------- search index helpers ----------

def reindex_lecture(db: Session, lecture: Lecture) -> None:
    db.execute(text("DELETE FROM search_index WHERE lecture_id = :lid"), {"lid": lecture.id})
    rows: list[dict[str, Any]] = []
    tj = lecture.transcript_json or {}
    for seg in tj.get("segments", []):
        rows.append({
            "lecture_id": lecture.id, "kind": "transcript", "anchor": seg["pid"],
            "title": seg.get("speaker") or "", "body": seg.get("text", ""),
        })
    notes = lecture.notes_json or {}
    for c in notes.get("concepts", []):
        body_parts = [c.get("plain_explanation", ""), *[b.get("text", "") for b in c.get("teaching_bullets", [])],
                      c.get("why_it_matters", ""), c.get("deeper_explanation", "")]
        rows.append({
            "lecture_id": lecture.id, "kind": "concept", "anchor": f"concept-{c['id']}",
            "title": f"Concept {c.get('number')}: {c.get('title', '')}", "body": "\n".join(body_parts),
        })
        for q in c.get("interview_questions", []):
            rows.append({
                "lecture_id": lecture.id, "kind": "interview", "anchor": f"concept-{c['id']}",
                "title": q.get("question", ""), "body": q.get("short_answer", ""),
            })
    for g in notes.get("glossary", []):
        rows.append({"lecture_id": lecture.id, "kind": "glossary", "anchor": "glossary",
                     "title": g.get("term", ""), "body": g.get("explanation", "")})
    if rows:
        db.execute(text(
            "INSERT INTO search_index(lecture_id, kind, anchor, title, body) "
            "VALUES (:lecture_id, :kind, :anchor, :title, :body)"), rows)


def search(db: Session, query: str, lecture_id: str | None = None, limit: int = 50) -> list[dict]:
    q = " ".join(f'"{t}"' for t in query.replace('"', " ").split() if t)
    if not q:
        return []
    sql = ("SELECT lecture_id, kind, anchor, title, snippet(search_index, 4, '<mark>', '</mark>', '…', 24) AS snip, "
           "bm25(search_index) AS rank FROM search_index WHERE search_index MATCH :q ")
    params: dict[str, Any] = {"q": q, "limit": limit}
    if lecture_id:
        sql += "AND lecture_id = :lid "
        params["lid"] = lecture_id
    sql += "ORDER BY rank LIMIT :limit"
    try:
        res = db.execute(text(sql), params).mappings().all()
    except Exception:
        return []
    return [dict(r) for r in res]


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
