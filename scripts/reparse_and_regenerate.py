"""Re-parse a lecture's transcript with the current parser and regenerate its notes from scratch.
Usage: PYTHONPATH=. .venv/bin/python scripts/reparse_and_regenerate.py <lecture_id>
"""
import sys, time
from pathlib import Path
from app import db as dbm
from app.jobs.runner import JobContext
from app.notes import pipeline
from app.parsers.transcript import assess_source, parse_file
from app.storage import lecture_dir

lid = sys.argv[1]
dbm.init_db()
with dbm.session() as s:
    lec = s.get(dbm.Lecture, lid)
    src = next(f for f in lec.files if f.kind in ("transcript", "pasted"))
    pt, _ = parse_file(lecture_dir(lid) / src.stored_path)
    lec.transcript_json = pt.to_json()
    lec.source_assessment = assess_source(pt)
    job = dbm.Job(lecture_id=lid, kind="generate_notes", params={"force": True}, status="running", started_at=dbm.now())
    s.add(job)
    s.commit()
    jid = job.id
    print(f"re-parsed: {len(pt.segments)} paragraphs; job {jid}")
t0 = time.time()
try:
    pipeline.generate_notes(JobContext(jid, lid), {"force": True})
    status = "succeeded"
except Exception as e:
    status = "failed"
    print("FAILED:", type(e).__name__, e)
with dbm.session() as s:
    job = s.get(dbm.Job, jid)
    job.status, job.finished_at = status, dbm.now()
    job.progress = 1.0 if status == "succeeded" else job.progress
    lec = s.get(dbm.Lecture, lid)
    lec.status = "processed" if lec.notes_json else "failed"
    lec.processing_seconds = (lec.processing_seconds or 0) + time.time() - t0
    s.commit()
    print(f"{status} in {time.time()-t0:.0f}s, job cost ${job.cost_usd:.2f}")
