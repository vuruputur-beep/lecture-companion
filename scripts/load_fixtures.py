"""Load the synthetic fixtures into the app and generate notes with the offline mock
provider, so the UI can be explored without an API key.

Run:  LC_PROVIDER=mock .venv/bin/python scripts/load_fixtures.py
Then: ./run.sh   (keep LC_PROVIDER=mock to regenerate offline, or set a real provider in Settings)
Everything produced is clearly labelled [FIXTURE].
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("LC_PROVIDER", "mock")
os.environ.setdefault("LC_VERIFY_LINKS", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbm, importer  # noqa: E402
from app.jobs.runner import JobContext  # noqa: E402
from app.notes import pipeline  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
dbm.init_db()
items = [("week4_rag_otter.txt", 4), ("week3_eval_untimed.txt", 3), ("week2_tooluse_summary.txt", 2)]
for name, week in items:
    with dbm.session() as s:
        lec = importer.create_lecture_from_transcript(s, course_name="Interview Kickstart - FDE (fixtures)", title=None, week=week,
                                                      lecture_date=None, filename=name, data=(FIX / name).read_bytes(), pasted_text=None)
        if week == 4:
            importer.attach_file(s, lec, "week4_slides.pdf", (FIX / "week4_slides.pdf").read_bytes())
            importer.attach_file(s, lec, "screenshot_fixture.png", (FIX / "screenshot_fixture.png").read_bytes())
        s.commit()
        lid = lec.id
    with dbm.session() as s:
        job = dbm.Job(lecture_id=lid, kind="generate_notes", params={}, status="running")
        s.add(job)
        s.commit()
        jid = job.id
    pipeline.generate_notes(JobContext(jid, lid), {})
    with dbm.session() as s:
        j = s.get(dbm.Job, jid)
        j.status, j.progress, j.finished_at = "succeeded", 1.0, dbm.now()
        s.get(dbm.Lecture, lid).status = "processed"
        s.commit()
    print(f"loaded {name} -> lecture {lid}")
print("Done. Start the app with ./run.sh and open http://127.0.0.1:8765")
