"""Deterministic offline provider used by tests and the `--fixtures` demo.

It produces schema-valid output derived from the transcript text itself, so the
whole import → generate → edit → export path can be verified without
credentials. Output is clearly labelled as fixture content.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .base import LLMResult, Provider


def _refs_in(text: str) -> list[str]:
    return re.findall(r"\[(P\d+)(?: @[\d:]+)?\]", text)


def _sentences(text: str, n: int) -> list[str]:
    body = re.sub(r"\[P\d+[^\]]*\]\s*(?:[\w .'-]+:)?", " ", text)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if len(s.split()) > 4]
    return sents[:n] or ["(no content in fixture)"]


class MockProvider(Provider):
    name = "mock"

    def __init__(self, model: str = "mock-fixture", **kwargs: Any):
        super().__init__(model, **kwargs)
        self.calls = 0

    def check(self):
        return {"ok": True, "detail": "Mock provider (fixtures only - no real model calls)."}

    def generate_json(self, system, user, schema, max_tokens=32000, step=""):
        self.calls += 1
        t0 = time.time()
        import os
        delay = float(os.environ.get("LC_MOCK_DELAY", "0") or 0)
        if delay:
            time.sleep(delay)
        text = user if isinstance(user, str) else " ".join(b.get("text", "") for b in user if b.get("type") == "text")
        full_text = text
        for marker in ("TRANSCRIPT EXCERPT", "TRANSCRIPT CHUNK"):
            if marker in text:
                text = text.split(marker, 1)[1]
        title = schema.get("title", "")
        data: dict[str, Any]
        if title == "ChunkInventory" or step.startswith("inventory"):
            refs = _refs_in(text) or ["P1"]
            step_n = max(1, len(refs) // 3)
            items = []
            kinds = ["concept", "demonstration", "qa", "concept"]
            for i in range(0, len(refs), step_n):
                grp = refs[i:i + step_n]
                k = kinds[(i // step_n) % len(kinds)]
                items.append({"title": f"[FIXTURE] Topic around {grp[0]}", "kind": k,
                              "summary": _sentences(text, 40)[min(i // step_n, 39)],
                              "start_ref": grp[0], "end_ref": grp[-1], "importance": "major" if k == "concept" else "minor"})
            data = {"chunk_summary": "[FIXTURE] " + " ".join(_sentences(text, 3)), "items": items[:6],
                    "terms": [{"term": w, "ref": refs[0]} for w in sorted({w for w in re.findall(r"\b[A-Z]{3,}\b", text)})[:8]],
                    "papers": [{"wording": "Attention Is All You Need", "ref": refs[0], "context": "fixture mention"}] if "attention" in text.lower() else [],
                    "resources": [{"wording": "Hugging Face", "url_text": "huggingface.co", "ref": refs[0], "purpose": "models"}] if "hugging" in text.lower() else [],
                    "flags": [{"kind": "unclear_speech", "note": "[FIXTURE] example flag", "refs": [refs[-1]]}],
                    "next_topic_announcement": "", "instructor_speaker_label": "Speaker 1"}
        elif title == "ConceptOutline" or step == "outline":
            spans = re.findall(r"\((P\d+)–(P\d+)\)", full_text)
            spans = [s for s in spans if s[0] != s[1]] or [("P1", "P2")]
            concepts = []
            for i, (a, b) in enumerate(spans[:24], start=1):
                concepts.append({"id": f"c{i}", "number": i, "title": f"[FIXTURE] Concept covering {a}-{b}",
                                 "scope": "Fixture scope.", "start_ref": a, "end_ref": b, "extra_refs": [],
                                 "grouped_mentions": [], "is_substantive": True})
            data = {"concepts": concepts, "rationale": "fixture"}
        elif title == "Explanation" or step.startswith("explain"):
            data = {"explanation": "[FIXTURE] Plain-words explanation of the selected passage."}
        elif title == "ConceptPart":
            props = schema.get("properties", {})
            full = self.generate_json(system, user.replace("TASK: Regenerate ONLY", "TASK: Write") if isinstance(user, str) else user, {"title": "Concept"}, max_tokens, step="concept:x").data
            data = {k: full.get(k) for k in props}
            if "interview_questions" in data:
                data["interview_questions"][0]["question"] = "[FIXTURE] regenerated question"
        elif title == "Concept" or step.startswith("concept"):
            m = re.search(r'id must be "(c\d+)", number (\d+)', full_text)
            cid, num = (m.group(1), int(m.group(2))) if m else ("c1", 1)
            refs = _refs_in(text) or ["P1"]
            sents = _sentences(text, 30)
            cited = lambda s, i: {"text": s, "provenance": "taught", "refs": [refs[min(i, len(refs) - 1)]]}
            data = {
                "id": cid, "number": num, "title": f"[FIXTURE] Concept {num}", "time_range": f"{refs[0]} - {refs[-1]}",
                "refs": [refs[0], refs[-1]],
                "in_one_breath": "[FIXTURE] one breath.", "plain_explanation": "[FIXTURE] " + " ".join(sents[:3]),
                "teaching_bullets": [cited(s, i) for i, s in enumerate(sents[:12])] or [cited("fixture", 0)],
                "definitions": [{"term": "Fixture term", "definition": "A placeholder definition.", "provenance": "additional", "refs": []}],
                "why_it_matters": "[FIXTURE] why", "when_used": "[FIXTURE] when", "analogy": "[FIXTURE] analogy",
                "analogy_limits": "[FIXTURE] limits", "steps": ["Step one", "Step two"],
                "examples": [{"title": "Fixture example", "body": "Body", "code": "print('hello')", "language": "python",
                              "provenance": "additional", "refs": [], "image_ids": []}],
                "pitfalls": {"assumptions": [cited("assumption", 0)], "tradeoffs": [cited("tradeoff", 1)],
                             "limitations": [], "common_mistakes": [{"text": "mistake", "provenance": "additional", "refs": []}]},
                "connections": "[FIXTURE] connections", "deeper_explanation": "[FIXTURE] deeper",
                "fde_applications": [{"text": "[FIXTURE] FDE application", "provenance": "additional", "refs": []}],
                "moments": [{"kind": "qa", "summary": "[FIXTURE] A student asked a question.", "detail": "", "refs": [refs[-1]]},
                            {"kind": "demonstration", "summary": "[FIXTURE] Demo", "detail": "", "refs": [refs[0]]}],
                "brief_mentions": [], "interview_questions": [{
                    "question": f"[FIXTURE] Explain concept {num}?", "short_answer": "Short answer.", "deep_answer": "Deep answer.",
                    "follow_ups": [{"question": "Follow-up?", "suggested_answer": "Answer."}], "conditions": "Conditions.",
                    "source": "generated", "refs": []}],
                "flags": [{"kind": "missing_visual", "note": "[FIXTURE] code shown on screen not in transcript", "refs": [refs[0]]}],
                "is_substantive": True, "image_ids": [], "user_edited_fields": [],
            }
        elif title == "Wrapup" or step == "wrapup":
            cids = re.findall(r"\((c\d+)\)", full_text) or ["c1"]
            data = {
                "overview": {"learning_objectives": ["[FIXTURE] objective 1", "objective 2"], "prerequisites": ["Python basics"],
                             "introduction": "[FIXTURE] Introduction.",
                             "takeaways": [{"text": f"[FIXTURE] takeaway {i}", "provenance": "taught", "refs": ["P1"]} for i in range(1, 11)],
                             "foundations": [{"term": "Token", "plain_explanation": "[FIXTURE] a chunk of text.", "why_needed_here": "everywhere", "everyday_analogy": "lego bricks"}],
                             "big_picture": {"one_paragraph_story": "[FIXTURE] story", "flow": ["Step 1 (Concept 1): a", "Step 2 (Concept 2): b"], "mental_model": "[FIXTURE] model", "where_this_fits": "[FIXTURE] fits"},
                             "user_edited_fields": []},
                "summary": {"main_lessons": ["[FIXTURE] lesson"], "how_they_connect": "They connect.", "next_topic_announced": "",
                            "next_topic_was_announced": False, "suggested_next_steps": ["Review"], "user_edited_fields": []},
                "practice": {"recall": [{"question": "[FIXTURE] Q?", "answer": "A.", "level": lvl, "concept_id": cids[0]} for lvl in ("basic", "application", "interview")],
                             "exercises": [{"title": "[FIXTURE] Exercise", "task": "Do it.", "hints": ["hint"], "solution_outline": "outline", "concept_ids": cids[:1]}],
                             "progression_note": "Basic → application → interview."},
                "glossary": [{"term": "RAG", "explanation": "[FIXTURE] Retrieval-augmented generation.", "concept_id": cids[0], "source": "speech", "refs": ["P1"]}],
                "insufficiency_note": "" if "NOT a full transcript" not in full_text else "[FIXTURE] The supplied material is a summary, not a full transcript.",
                "global_flags": [],
            }
        elif step == "mentions":
            data = {"papers": [{"title_as_heard": "Attention Is All You Need", "identified_title": "", "authors": "", "year": "", "link": "",
                                "link_verified": False, "confidence": "unidentified", "why_it_matters": "[FIXTURE]", "refs": ["P1"], "is_additional_reading": False}],
                    "resources": [{"name": "Hugging Face", "kind": "website", "url_as_heard": "huggingface.co", "url": "", "url_verified": False,
                                   "uncertain": False, "purpose": "[FIXTURE] models", "refs": ["P1"], "is_additional": False}]}
        elif step == "guide":
            lids = re.findall(r"lecture_id=([a-f0-9]{12})", text)
            cids = re.findall(r"concept_id=(c\d+)", text)
            l0, c0 = (lids[0] if lids else "x"), (cids[0] if cids else "c1")
            data = {"title": "[FIXTURE] Combined study guide", "story": "[FIXTURE] story",
                    "themes": [{"theme": "[FIXTURE] theme", "summary": "s", "concepts": [{"lecture_id": l0, "concept_id": c0, "title": "t"}]}],
                    "foundations": [{"term": "Token", "plain_explanation": "chunk", "why_needed_here": "", "everyday_analogy": ""}],
                    "learning_path": [{"step": 1, "title": "Start", "what_to_do": "read", "lecture_id": l0, "concept_ids": [c0], "minutes": 20}],
                    "interview_bank": [{"theme": "[FIXTURE] theme", "questions": [{"lecture_id": l0, "concept_id": c0, "index": 0}]}],
                    "overlaps": ["[FIXTURE] overlap"], "gaps": ["[FIXTURE] gap"],
                    "glossary": [{"term": "RAG", "explanation": "x", "concept_id": c0, "source": "speech", "refs": [l0]}]}
        elif step.startswith("image"):
            data = {"caption": "[FIXTURE] image caption", "ocr_text": "fixture text", "kind": "slide", "legibility": "clear"}
        else:
            data = {}
        raw = json.dumps(data)
        return LLMResult(data=data, raw_text=raw, tokens_in=len(text) // 4, tokens_out=len(raw) // 4, cost_usd=0.0,
                         seconds=time.time() - t0, model=self.model, provider=self.name, stop_reason="end_turn")
