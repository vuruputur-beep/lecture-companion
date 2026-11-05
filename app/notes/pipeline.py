"""Notes generation pipeline (job handlers).

Steps (each stored under data/lectures/<id>/pipeline/ so restarts and
regenerations never redo finished work):
  images     - caption/OCR attached slides & screenshots (vision)
  inventory  - per-chunk content inventory with context carried across chunks
  outline    - merged, ordered concept outline
  concept:<id> - detailed entry per concept
  mentions   - papers & resources consolidated, then verified programmatically
  wrapup     - overview, summary, practice, glossary
  coverage   - programmatic coverage report
"""
from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func

from .. import db as dbm
from ..config import load_config
from ..jobs.runner import JobContext, register
from ..parsers.transcript import format_timestamp
from ..providers import ProviderError, get_provider
from ..providers.base import LLMResult, SpendLimitError
from ..storage import lecture_dir, read_pipeline, write_pipeline
from . import links, prompts
from .schema import (ChunkInventory, Concept, ConceptOutline, Coverage, CoverageGap, ImageRef,
                     LectureNotes, Paper, Resource, Wrapup, strict_schema)

log = logging.getLogger("lc.pipeline")

REF_RE = re.compile(r"P(\d+)")


# ---------------------------------------------------------------- helpers

def ref_to_idx(ref: str) -> int | None:
    m = REF_RE.search(ref or "")
    return int(m.group(1)) - 1 if m else None


def refs_to_indices(refs: list[str]) -> set[int]:
    out: set[int] = set()
    for r in refs or []:
        if "-" in r and r.count("P") >= 2:
            a, b = r.split("-", 1)
            ia, ib = ref_to_idx(a), ref_to_idx(b)
            if ia is not None and ib is not None:
                out.update(range(min(ia, ib), max(ia, ib) + 1))
                continue
        i = ref_to_idx(r)
        if i is not None:
            out.add(i)
    return out


def seg_label(seg: dict[str, Any]) -> str:
    tag = seg["pid"]
    if seg.get("start") is not None:
        tag += f" @{format_timestamp(seg['start'])}"
    return tag


def render_segments(segs: list[dict[str, Any]]) -> str:
    lines = []
    for s in segs:
        spk = f" {s['speaker']}:" if s.get("speaker") else ""
        lines.append(f"[{seg_label(s)}]{spk} {s['text']}")
    return "\n".join(lines)


def make_chunks(segments: list[dict[str, Any]], chunk_words: int) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    count = 0
    for s in segments:
        w = len(s["text"].split())
        if cur and count + w > chunk_words:
            chunks.append(cur)
            cur, count = [], 0
        cur.append(s)
        count += w
    if cur:
        chunks.append(cur)
    # avoid a tiny trailing chunk
    if len(chunks) > 1 and sum(len(s["text"].split()) for s in chunks[-1]) < chunk_words * 0.2:
        chunks[-2].extend(chunks.pop())
    return chunks


class Budget:
    def __init__(self, ctx: JobContext, lecture_id: str, step_prefix: str = ""):
        self.ctx = ctx
        self.lecture_id = lecture_id
        cfg = load_config()
        self.per_lecture = float(cfg.get("spend_limit_usd_per_lecture", 15.0))
        self.total = float(cfg.get("spend_limit_usd_total", 200.0))
        self.provider_name = cfg.get("provider")
        self.model = cfg.get("model")

    def record(self, step: str, res: LLMResult) -> None:
        self.ctx.add_cost(res.cost_usd, res.tokens_in, res.tokens_out)
        with dbm.session() as s:
            s.add(dbm.ApiCallLog(lecture_id=self.lecture_id, job_id=self.ctx.job_id, step=step,
                                 provider=res.provider, model=res.model, tokens_in=res.tokens_in,
                                 tokens_out=res.tokens_out, cache_read=res.cache_read, cache_write=res.cache_write,
                                 cost_usd=res.cost_usd, seconds=res.seconds))
            s.commit()

    def check(self) -> None:
        with dbm.session() as s:
            lec = s.get(dbm.Lecture, self.lecture_id)
            lecture_cost = float(lec.cost_usd or 0) if lec else 0.0
            total = s.query(func.sum(dbm.ApiCallLog.cost_usd)).scalar()
        if lecture_cost >= self.per_lecture:
            raise SpendLimitError(f"Per-lecture spending limit reached (${lecture_cost:.2f} ≥ ${self.per_lecture:.2f}). "
                                  "Raise the limit in Settings and retry to continue from the checkpoint.")
        if total is not None and float(total or 0) >= self.total:
            raise SpendLimitError(f"Total spending limit reached (${float(total):.2f}). Raise it in Settings to continue.")


def call(provider, budget: Budget, step: str, system: str, user: Any, model_cls, max_tokens: int = 32000,
         retries: int = 2) -> tuple[Any, LLMResult]:
    """Call the provider with retries on invalid JSON/validation and record cost."""
    budget.check()
    schema = strict_schema(model_cls)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            res = provider.generate_json(system, user, schema, max_tokens=max_tokens, step=step)
            budget.record(step, res)
            try:
                obj = model_cls.model_validate(res.data)
            except ValidationError as ve:
                last_err = ve
                log.warning("validation error in %s (attempt %d): %s", step, attempt, str(ve)[:500])
                continue
            return obj, res
        except ProviderError as pe:
            last_err = pe
            if "invalid JSON" in str(pe) and attempt < retries:
                continue
            raise
    raise ProviderError(f"Step {step} failed after retries: {last_err}")


def lecture_meta(lecture_id: str) -> dict[str, Any]:
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lecture_id)
        if not lec:
            raise RuntimeError("lecture not found")
        files = [{"id": f.id, "kind": f.kind, "name": f.original_name, "path": f.stored_path, "mime": f.mime,
                  "meta": f.meta or {}} for f in lec.files]
        return {
            "id": lec.id, "title": lec.title, "course": lec.course.name if lec.course else "",
            "week": lec.week, "date": lec.lecture_date, "transcript": lec.transcript_json or {},
            "assessment": lec.source_assessment or {}, "files": files, "notes": lec.notes_json,
        }


# ---------------------------------------------------------------- image captions

def step_images(ctx: JobContext, provider, budget: Budget, meta: dict[str, Any], force: bool = False) -> list[dict[str, Any]]:
    existing = read_pipeline(meta["id"], "images")
    if existing is not None and not force:
        return existing
    ldir = lecture_dir(meta["id"])
    image_files = [f for f in meta["files"] if f["kind"] in ("image", "screenshot", "slides_page", "slides")]
    # per-image checkpoint so a restart resumes instead of re-describing finished images
    partial = read_pipeline(meta["id"], "images_partial") if not force else None
    images: list[dict[str, Any]] = list(partial or [])
    done_paths = {im["path"] for im in images}
    n = 0
    for f in image_files:
        p = ldir / f["path"]
        if not p.exists() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            continue
        n += 1
        if n > 80:
            break
        iid = f"IMG{n}"
        if f["path"] in done_paths:
            continue
        ctx.progress(0.02 + 0.06 * n / max(1, len(image_files)), f"Describing image {n}/{len(image_files)}: {f['name']}")
        media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}[p.suffix.lower()[1:]]
        b64 = base64.standard_b64encode(p.read_bytes()).decode()
        from pydantic import BaseModel

        class ImageDesc(BaseModel):
            caption: str
            ocr_text: str
            kind: str
            legibility: str

        try:
            desc, _ = call(provider, budget, f"image:{iid}", prompts.SYSTEM,
                           [{"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                            {"type": "text", "text": prompts.IMAGE_USER.format(title=meta["title"])}],
                           ImageDesc, max_tokens=4000)
            d = desc.model_dump()
        except ProviderError as e:
            d = {"caption": f"(description failed: {e})", "ocr_text": "", "kind": "other", "legibility": "unknown"}
        images.append({
            "id": iid, "path": f["path"], "source_file": f["name"], "page": (f.get("meta") or {}).get("page"),
            "timestamp": (f.get("meta") or {}).get("timestamp"),  # only set when the source carries one
            "caption": d["caption"], "ocr_text": d["ocr_text"], "kind": d["kind"], "legibility": d["legibility"],
        })
        write_pipeline(meta["id"], "images_partial", images)
    write_pipeline(meta["id"], "images", images)
    return images


def visual_context(images: list[dict[str, Any]], limit_chars: int = 12000) -> str:
    if not images:
        return ""
    lines = ["VISUAL CONTEXT (attached slides/screenshots; reference by id in image_ids; a timestamp is given only when the source had one):"]
    for im in images:
        src = f"{im['source_file']}" + (f" p.{im['page']}" if im.get("page") else "")
        ts = f" @{format_timestamp(im['timestamp'])}" if im.get("timestamp") is not None else ""
        ocr = im.get("ocr_text", "").strip()
        ocr = (ocr[:600] + "…") if len(ocr) > 600 else ocr
        lines.append(f"- {im['id']} ({src}{ts}, {im.get('kind')}): {im['caption']}" + (f"\n    text: {ocr}" if ocr else ""))
    out = "\n".join(lines)
    return out[:limit_chars]


# ---------------------------------------------------------------- inventory & outline

def step_inventory(ctx, provider, budget, meta, chunks, vis, force=False) -> list[dict[str, Any]]:
    inv = read_pipeline(meta["id"], "inventories") or []
    if len(inv) == len(chunks) and not force:
        return inv
    inv = [i for i in inv if i is not None][: len(chunks)]
    for ci in range(len(inv), len(chunks)):
        ctx.progress(0.08 + 0.22 * ci / len(chunks), f"Inventory: chunk {ci + 1}/{len(chunks)}")
        prior = ""
        if inv:
            prior = "CONTEXT FROM EARLIER CHUNKS:\n" + "\n".join(
                f"- chunk {i + 1}: {x['chunk_summary']}" for i, x in enumerate(inv[-4:], start=max(0, len(inv) - 4)))
        user = prompts.INVENTORY_USER.format(
            chunk_no=ci + 1, chunk_total=len(chunks), title=meta["title"], course=meta["course"],
            week=meta["week"] or "?", prior_context=prior, visual_context=vis, chunk_text=render_segments(chunks[ci]))
        obj, _ = call(provider, budget, f"inventory:{ci + 1}", prompts.SYSTEM, user, ChunkInventory, max_tokens=16000)
        d = obj.model_dump()
        d["chunk_index"] = ci
        d["first_ref"], d["last_ref"] = chunks[ci][0]["pid"], chunks[ci][-1]["pid"]
        inv.append(d)
        write_pipeline(meta["id"], "inventories", inv)
        ctx.checkpoint({"step": "inventory", "done": len(inv)})
    return inv


def step_outline(ctx, provider, budget, meta, inventories, force=False) -> dict[str, Any]:
    existing = read_pipeline(meta["id"], "outline")
    if existing and not force:
        return existing
    ctx.progress(0.31, "Building the concept outline")
    digest = []
    for inv in inventories:
        digest.append(f"## Chunk {inv['chunk_index'] + 1} ({inv['first_ref']}–{inv['last_ref']})\nSummary: {inv['chunk_summary']}")
        for it in inv["items"]:
            digest.append(f"- [{it['kind']}/{it['importance']}] {it['title']} ({it['start_ref']}–{it['end_ref']}): {it['summary']}")
    user = prompts.OUTLINE_USER.format(title=meta["title"], assessment=meta["assessment"], inventories="\n".join(digest))
    obj, _ = call(provider, budget, "outline", prompts.SYSTEM, user, ConceptOutline, max_tokens=16000)
    outline = obj.model_dump()
    # normalise ids/numbers
    for i, c in enumerate(outline["concepts"], start=1):
        c["id"], c["number"] = f"c{i}", i
    write_pipeline(meta["id"], "outline", outline)
    ctx.checkpoint({"step": "outline"})
    return outline


# ---------------------------------------------------------------- concepts

def concept_excerpt(segments: list[dict[str, Any]], oc: dict[str, Any], margin: int = 2, max_words: int = 14000) -> str:
    a, b = ref_to_idx(oc["start_ref"]), ref_to_idx(oc["end_ref"])
    if a is None:
        a = 0
    if b is None or b < a:
        b = min(len(segments) - 1, a + 40)
    idxs = set(range(max(0, a - margin), min(len(segments), b + margin + 1)))
    for r in oc.get("extra_refs", []):
        i = ref_to_idx(r)
        if i is not None:
            idxs.update(range(max(0, i - 1), min(len(segments), i + 4)))
    segs = [segments[i] for i in sorted(idxs)]
    # cap size, keeping the primary span first
    words = 0
    kept = []
    for s in segs:
        w = len(s["text"].split())
        if words + w > max_words:
            break
        kept.append(s)
        words += w
    return render_segments(kept)


def step_concept(ctx, provider, budget, meta, outline, oc, segments, vis, inventories, force=False) -> dict[str, Any]:
    name = f"concept_{oc['id']}"
    existing = read_pipeline(meta["id"], name)
    if existing and not force:
        return existing
    earlier = "; ".join(f"Concept {c['number']}: {c['title']}" for c in outline["concepts"] if c["number"] < oc["number"]) or "none (this is the first concept)"
    extra = f"; also revisited at {', '.join(oc['extra_refs'])}" if oc.get("extra_refs") else ""
    # restrict visual context to images whose timestamps fall in range when timestamps exist; else pass all
    user = prompts.CONCEPT_USER.format(
        number=oc["number"], title=oc["title"], lecture_title=meta["title"], course=meta["course"], week=meta["week"] or "?",
        scope=oc["scope"], start_ref=oc["start_ref"], end_ref=oc["end_ref"], extra=extra, earlier=earlier,
        mentions="; ".join(oc.get("grouped_mentions", [])) or "none", visual_context=vis, cid=oc["id"],
        is_substantive=str(oc.get("is_substantive", True)).lower(),
        excerpt=concept_excerpt(segments, oc))
    obj, _ = call(provider, budget, f"concept:{oc['id']}", prompts.SYSTEM, user, Concept, max_tokens=24000)
    d = obj.model_dump()
    d["id"], d["number"], d["title"] = oc["id"], oc["number"], d["title"] or oc["title"]
    if not d.get("refs"):
        d["refs"] = [oc["start_ref"], oc["end_ref"]]
    write_pipeline(meta["id"], name, d)
    return d


def step_all_concepts(ctx, provider, budget, meta, outline, segments, vis, inventories, force=False, parallelism=3) -> list[dict[str, Any]]:
    """Generate every concept entry; independent concepts run concurrently (file checkpoints per concept)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ocs = outline["concepts"]
    total = len(ocs)
    results: dict[str, dict[str, Any]] = {}
    done = 0
    ctx.progress(0.34, f"Writing {total} concept entries ({parallelism} at a time)")
    with ThreadPoolExecutor(max_workers=max(1, parallelism)) as ex:
        futs = {ex.submit(step_concept, ctx, provider, budget, meta, outline, oc, segments, vis, inventories, force): oc for oc in ocs}
        for fut in as_completed(futs):
            oc = futs[fut]
            results[oc["id"]] = fut.result()
            done += 1
            ctx.progress(0.34 + 0.5 * done / max(1, total), f"Concept {oc['number']}/{total} done: {oc['title']}")
            ctx.checkpoint({"step": "concepts", "done": done, "total": total})
    return [results[oc["id"]] for oc in ocs]


# ---------------------------------------------------------------- mentions / wrapup

def step_mentions(ctx, provider, budget, meta, inventories, force=False) -> dict[str, Any]:
    existing = read_pipeline(meta["id"], "mentions")
    if existing and not force:
        return existing
    ctx.progress(0.86, "Consolidating papers and resources")
    papers = [p for inv in inventories for p in inv.get("papers", [])]
    resources = [r for inv in inventories for r in inv.get("resources", [])]

    from pydantic import BaseModel, Field

    class Mentions(BaseModel):
        papers: list[Paper] = Field(default_factory=list)
        resources: list[Resource] = Field(default_factory=list)

    if not papers and not resources:
        data = {"papers": [], "resources": []}
    else:
        user = prompts.MENTIONS_USER.format(
            title=meta["title"],
            papers="\n".join(f"- [{p['ref']}] \"{p['wording']}\" - {p.get('context', '')}" for p in papers) or "(none)",
            resources="\n".join(f"- [{r['ref']}] {r['wording']} url_text={r.get('url_text') or '-'} purpose={r.get('purpose', '')}" for r in resources) or "(none)")
        obj, _ = call(provider, budget, "mentions", prompts.SYSTEM, user, Mentions, max_tokens=12000)
        data = obj.model_dump()
    # programmatic verification
    cfg = load_config()
    if cfg.get("verify_links", True):
        ctx.progress(0.9, "Verifying papers and links")
        for p in data["papers"]:
            if p.get("is_additional_reading") and p.get("identified_title"):
                q = p["identified_title"]
            else:
                q = p["title_as_heard"]
            found = links.identify_paper(q)
            if found.get("confidence") in ("confident", "uncertain"):
                p.update({"identified_title": found["title"], "authors": found["authors"], "year": found["year"],
                          "link": found["link"], "link_verified": found["link_verified"], "confidence": found["confidence"]})
            else:
                p["confidence"] = "unidentified"
                p["link"], p["link_verified"] = "", False
        for r in data["resources"]:
            url = r.get("url") or links.normalize_url_guess(r.get("url_as_heard", ""))
            if url:
                r["url"] = url
                r["url_verified"] = links.verify_url(url)
                if not r["url_verified"]:
                    r["uncertain"] = True
            else:
                r["url_verified"] = False
    write_pipeline(meta["id"], "mentions", data)
    return data


def step_wrapup(ctx, provider, budget, meta, concepts, inventories, force=False) -> dict[str, Any]:
    existing = read_pipeline(meta["id"], "wrapup")
    if existing and not force:
        return existing
    ctx.progress(0.92, "Writing overview, summary, glossary and practice")
    digest = []
    for c in concepts:
        bullets = "\n".join(f"    • {b['text']} [{b['provenance']}; {', '.join(b['refs'][:2])}]" for b in c["teaching_bullets"][:12])
        terms = ", ".join(d["term"] for d in c.get("definitions", []))
        digest.append(f"Concept {c['number']} ({c['id']}): {c['title']} [{c.get('time_range', '')}]\n  {c['plain_explanation'][:700]}\n{bullets}\n  terms: {terms}")
    terms = []
    for inv in inventories:
        for t in inv.get("terms", []):
            terms.append(f"{t['term']} -> {t['ref']}")
    nexts = [inv["next_topic_announcement"] for inv in inventories if inv.get("next_topic_announcement")]
    a = meta["assessment"]
    insuff = ""
    if not a.get("sufficient_for_comprehensive_notes", True):
        insuff = ("NOTE: the supplied material is NOT a full transcript or is very short (" + a.get("kind", "") +
                  f", {a.get('word_count')} words). Explain this in insufficiency_note and keep claims to what was supplied.")
    user = prompts.WRAPUP_USER.format(
        title=meta["title"], course=meta["course"], week=meta["week"] or "?", assessment=a, insufficiency=insuff,
        concept_digest="\n\n".join(digest), chunk_summaries="\n".join(f"{i + 1}. {inv['chunk_summary']}" for i, inv in enumerate(inventories)),
        next_topic=" | ".join(nexts) if nexts else "(none)", terms="; ".join(terms[:300]))
    obj, _ = call(provider, budget, "wrapup", prompts.SYSTEM, user, Wrapup, max_tokens=24000)
    data = obj.model_dump()
    write_pipeline(meta["id"], "wrapup", data)
    return data


# ---------------------------------------------------------------- coverage

def compute_coverage(segments: list[dict[str, Any]], concepts: list[dict[str, Any]], inventories: list[dict[str, Any]],
                     chunk_count: int) -> dict[str, Any]:
    n = len(segments)
    referenced: set[int] = set()
    for c in concepts:
        span = refs_to_indices(c.get("refs", []))
        if len(span) == 2:  # treat [start, end] as a range
            a, b = sorted(span)
            span = set(range(a, b + 1))
        referenced |= span
        for field in ("teaching_bullets", "definitions", "examples", "moments", "fde_applications", "brief_mentions"):
            for item in c.get(field, []):
                referenced |= refs_to_indices(item.get("refs", []))
        for grp in c.get("pitfalls", {}).values():
            for item in grp:
                referenced |= refs_to_indices(item.get("refs", []))
    referenced = {i for i in referenced if 0 <= i < n}
    gaps: list[dict[str, Any]] = []
    if n:
        run_start = None
        for i in range(n + 1):
            missing = i < n and i not in referenced
            if missing and run_start is None:
                run_start = i
            if (not missing) and run_start is not None:
                words = sum(len(segments[j]["text"].split()) for j in range(run_start, i))
                if words >= 350:
                    a, b = segments[run_start], segments[i - 1]
                    sev = "high" if words > 1500 else ("medium" if words > 700 else "low")
                    gaps.append({"refs": [a["pid"], b["pid"]],
                                 "description": f"{words} words ({seg_label(a)} → {seg_label(b)}) are not referenced by any concept. "
                                                f"Starts: “{a['text'][:140]}…”", "severity": sev})
                run_start = None
    third = max(1, n // 3)
    def covered(lo, hi):
        rng = range(lo, min(hi, n))
        return bool(rng) and len([i for i in rng if i in referenced]) / len(rng) >= 0.4
    demo_items = [it for inv in inventories for it in inv["items"] if it["kind"] in ("demonstration", "troubleshooting")]
    qa_items = [it for inv in inventories for it in inv["items"] if it["kind"] == "qa"]
    moments = [m for c in concepts for m in c.get("moments", [])]
    notes = []
    if not demo_items:
        notes.append("No demonstrations were detected in the transcript inventory.")
    if not qa_items:
        notes.append("No student Q&A was detected in the transcript inventory.")
    return Coverage(
        segments_total=n, segments_referenced=len(referenced), beginning_covered=covered(0, third),
        middle_covered=covered(third, 2 * third), end_covered=covered(2 * third, n),
        demonstrations_covered=(not demo_items) or any(m["kind"] in ("demonstration", "troubleshooting") for m in moments),
        qa_covered=(not qa_items) or any(m["kind"] == "qa" for m in moments),
        gaps=[CoverageGap(**g) for g in gaps], chunk_count=chunk_count, notes=notes).model_dump()


# ---------------------------------------------------------------- assembly

def assemble(meta, concepts, wrapup, mentions, images, coverage, model_name: str, previous: dict | None = None) -> dict[str, Any]:
    notes = LectureNotes(
        version=(previous or {}).get("version", 0) + 1,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=model_name, source_kind=meta["assessment"].get("kind", ""),
        insufficiency_note=wrapup.get("insufficiency_note", ""),
        overview=wrapup["overview"], concepts=concepts, glossary=wrapup["glossary"],
        papers=mentions.get("papers", []), resources=mentions.get("resources", []),
        summary=wrapup["summary"], practice=wrapup["practice"], coverage=coverage,
        images=[ImageRef(id=i["id"], path=i["path"], caption=i.get("caption", ""), source_file=i.get("source_file", ""),
                         page=i.get("page"), timestamp=i.get("timestamp"), ocr_text=i.get("ocr_text", "")) for i in images],
        global_flags=wrapup.get("global_flags", []),
    )
    return notes.model_dump()


def preserve_user_edits(new: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    """Copy user-edited fields from the old notes into the regenerated ones."""
    if not old:
        return new
    for sec in ("overview", "summary"):
        for f in (old.get(sec) or {}).get("user_edited_fields", []):
            if f in old[sec]:
                new[sec][f] = old[sec][f]
                new[sec]["user_edited_fields"] = sorted(set(new[sec].get("user_edited_fields", [])) | {f})
    old_concepts = {c["id"]: c for c in old.get("concepts", [])}
    for c in new.get("concepts", []):
        oc = old_concepts.get(c["id"])
        if not oc:
            continue
        for f in oc.get("user_edited_fields", []):
            if f in oc:
                c[f] = oc[f]
        if oc.get("user_edited_fields"):
            c["user_edited_fields"] = sorted(set(c.get("user_edited_fields", [])) | set(oc["user_edited_fields"]))
    return new


def save_notes(lecture_id: str, notes: dict[str, Any]) -> None:
    with dbm.session() as s:
        lec = s.get(dbm.Lecture, lecture_id)
        if not lec:
            return
        lec.notes_json = notes
        lec.notes_version = notes.get("version", 1)
        lec.coverage_json = notes.get("coverage")
        dbm.reindex_lecture(s, lec)
        s.commit()


# ---------------------------------------------------------------- job handlers

@register("generate_notes")
def generate_notes(ctx: JobContext, params: dict[str, Any]) -> None:
    t0 = time.time()
    meta = lecture_meta(ctx.lecture_id)
    cfg = load_config()
    provider = get_provider(cfg)
    budget = Budget(ctx, ctx.lecture_id)
    force = bool(params.get("force", False))
    segments = meta["transcript"].get("segments", [])
    if not segments:
        raise RuntimeError("Lecture has no transcript segments to process.")
    ctx.progress(0.01, f"Starting notes generation with {cfg['provider']} / {cfg['model']}")
    if force:
        for p in (lecture_dir(ctx.lecture_id) / "pipeline").glob("*.json"):
            p.unlink()
    images = step_images(ctx, provider, budget, meta, force=force)
    vis = visual_context(images)
    chunks = make_chunks(segments, int(cfg.get("chunk_words", 7000)))
    inventories = step_inventory(ctx, provider, budget, meta, chunks, vis, force=force)
    outline = step_outline(ctx, provider, budget, meta, inventories, force=force)
    concepts = step_all_concepts(ctx, provider, budget, meta, outline, segments, vis, inventories, force=force,
                                 parallelism=int(cfg.get("concept_parallelism", 3)))
    mentions = step_mentions(ctx, provider, budget, meta, inventories, force=force)
    wrapup = step_wrapup(ctx, provider, budget, meta, concepts, inventories, force=force)
    ctx.progress(0.97, "Computing coverage report")
    coverage = compute_coverage(segments, concepts, inventories, len(chunks))
    notes = assemble(meta, concepts, wrapup, mentions, images, coverage, f"{cfg['provider']}:{cfg['model']}", meta.get("notes"))
    notes = preserve_user_edits(notes, meta.get("notes"))
    save_notes(ctx.lecture_id, notes)
    ctx.progress(1.0, f"Notes ready: {len(concepts)} concepts, {len(wrapup['glossary'])} glossary terms, "
                      f"{time.time() - t0:.0f}s, ${ctx.job_cost():.2f}")


@register("regenerate_sections")
def regenerate_sections(ctx: JobContext, params: dict[str, Any]) -> None:
    """Regenerate selected sections only. params: {"sections": [...], "overwrite_edits": bool}
    Section keys: overview, summary, practice, glossary, papers_resources, concept:<id>, coverage."""
    meta = lecture_meta(ctx.lecture_id)
    old = meta.get("notes")
    if not old:
        raise RuntimeError("No existing notes; run full generation first.")
    cfg = load_config()
    provider = get_provider(cfg)
    budget = Budget(ctx, ctx.lecture_id)
    sections = list(params.get("sections", []))
    overwrite = bool(params.get("overwrite_edits", False))
    segments = meta["transcript"].get("segments", [])
    chunks = make_chunks(segments, int(cfg.get("chunk_words", 7000)))
    images = read_pipeline(ctx.lecture_id, "images") or []
    vis = visual_context(images)
    inventories = step_inventory(ctx, provider, budget, meta, chunks, vis)
    outline = read_pipeline(ctx.lecture_id, "outline") or step_outline(ctx, provider, budget, meta, inventories)
    concepts = {c["id"]: c for c in old["concepts"]}
    n = len(sections)
    for i, sec in enumerate(sections):
        ctx.progress(0.1 + 0.8 * i / max(1, n), f"Regenerating {sec}")
        if sec.startswith("concept:"):
            cid = sec.split(":", 1)[1]
            oc = next((c for c in outline["concepts"] if c["id"] == cid), None)
            if not oc:
                continue
            concepts[cid] = step_concept(ctx, provider, budget, meta, outline, oc, segments, vis, inventories, force=True)
    ordered = [concepts[c["id"]] for c in outline["concepts"] if c["id"] in concepts]
    wrapup = read_pipeline(ctx.lecture_id, "wrapup") or {}
    if any(s in sections for s in ("overview", "summary", "practice", "glossary")):
        new_wrap = step_wrapup(ctx, provider, budget, meta, ordered, inventories, force=True)
        for k in ("overview", "summary", "practice", "glossary", "insufficiency_note", "global_flags"):
            if k in sections or k in ("insufficiency_note", "global_flags"):
                wrapup[k] = new_wrap[k]
        write_pipeline(ctx.lecture_id, "wrapup", wrapup)
    if not wrapup:
        wrapup = {"overview": old["overview"], "summary": old["summary"], "practice": old["practice"],
                  "glossary": old["glossary"], "insufficiency_note": old.get("insufficiency_note", ""), "global_flags": old.get("global_flags", [])}
    mentions = {"papers": old["papers"], "resources": old["resources"]}
    if "papers_resources" in sections:
        mentions = step_mentions(ctx, provider, budget, meta, inventories, force=True)
    coverage = compute_coverage(segments, ordered, inventories, len(chunks))
    notes = assemble(meta, ordered, wrapup, mentions, images, coverage, f"{cfg['provider']}:{cfg['model']}", old)
    # keep untouched sections exactly as they were (including user edits)
    for k in ("overview", "summary", "practice", "glossary"):
        if k not in sections:
            notes[k] = old[k]
    if not overwrite:
        notes = preserve_user_edits(notes, old)
    else:
        # drop edited markers for regenerated sections only
        for sec in sections:
            if sec.startswith("concept:"):
                for c in notes["concepts"]:
                    if c["id"] == sec.split(":", 1)[1]:
                        c["user_edited_fields"] = []
            elif sec in ("overview", "summary"):
                notes[sec]["user_edited_fields"] = []
        notes = preserve_user_edits(notes, {k: v for k, v in old.items() if k not in sections} | {"concepts": [c for c in old["concepts"] if f"concept:{c['id']}" not in sections]})
    notes["images"] = [ImageRef(id=i["id"], path=i["path"], caption=i.get("caption", ""), source_file=i.get("source_file", ""),
                                page=i.get("page"), timestamp=i.get("timestamp"), ocr_text=i.get("ocr_text", "")).model_dump() for i in images]
    save_notes(ctx.lecture_id, notes)
    ctx.progress(1.0, f"Regenerated {n} section(s), ${ctx.job_cost():.2f}")
