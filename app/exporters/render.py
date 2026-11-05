"""Exports rendered from the single LectureNotes model.

Markdown  -> text with images referenced by path (embedded as data URIs in HTML)
HTML      -> standalone, images embedded base64, print CSS
PDF       -> HTML printed by headless Chrome (bundled with macOS Chrome) or Chromium
DOCX      -> python-docx walking the same model, images embedded
Modes: "detailed" (everything) or "concise" (overview, short concept cards, glossary, summary).
"""
from __future__ import annotations

import base64
import html
import mimetypes
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from ..parsers.transcript import format_timestamp
from ..storage import lecture_dir

PROV_LABEL = {"taught": "Taught", "additional": "Additional explanation", "needs_verification": "Needs verification"}
PROV_SHORT = {"taught": "T", "additional": "A", "needs_verification": "?"}

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome", "chromium", "chromium-browser",
]


def find_chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
        w = shutil.which(c)
        if w:
            return w
    return None


# ------------------------------------------------------------------ markdown

def _refs(refs: list[str]) -> str:
    return f" ⟨{', '.join(refs)}⟩" if refs else ""


def _cited_list(items: list[dict[str, Any]], prov: bool = True) -> list[str]:
    out = []
    for it in items:
        tag = ""
        if prov and it.get("provenance") and it["provenance"] != "taught":
            tag = f" *[{PROV_LABEL[it['provenance']]}]*"
        out.append(f"- {it['text']}{tag}{_refs(it.get('refs', []))}")
    return out


def render_markdown(lecture: dict[str, Any], notes: dict[str, Any], mode: str = "detailed",
                    transcript: dict[str, Any] | None = None, image_base: str = "images") -> str:
    concise = mode == "concise"
    L: list[str] = []
    meta = f"{lecture.get('course', '')} · Week {lecture.get('week') or '?'} · {lecture.get('lecture_date') or 'date unknown'}"
    L += [f"# {lecture['title']}", "", f"*{meta}*", "",
          f"Generated {notes.get('generated_at', '')[:10]} with {notes.get('model', '')} · notes v{notes.get('version', 1)} · "
          f"source: {notes.get('source_kind', '')}", ""]
    L += ["> Legend: statements are **Taught** in the lecture unless marked *[Additional explanation]* or *[Needs verification]*. "
          "⟨P12⟩ cites transcript paragraph 12; ⟨P12@1:02:03⟩ adds the timestamp when the export had one.", ""]
    if notes.get("insufficiency_note"):
        L += ["> **Limited source material.** " + notes["insufficiency_note"], ""]
    if notes.get("global_flags"):
        L += ["> **Quality flags:** " + "; ".join(f"{f['kind']}: {f['note']}" for f in notes["global_flags"]), ""]

    ov = notes["overview"]
    L += ["## A. Lecture overview", "", "### Learning objectives", *[f"- {x}" for x in ov["learning_objectives"]], "",
          "### Prerequisites", *[f"- {x}" for x in ov["prerequisites"]], "", "### Introduction", ov["introduction"], "",
          "### Key takeaways", *_cited_list(ov["takeaways"]), ""]

    L += ["## B. Detailed concepts", ""]
    for c in notes["concepts"]:
        L += [f"### Concept {c['number']}: {c['title']}", ""]
        if c.get("time_range"):
            L += [f"*{c['time_range']}*", ""]
        L += ["**Plain-English explanation.** " + c["plain_explanation"], ""]
        bullets = c["teaching_bullets"][:5] if concise else c["teaching_bullets"]
        L += ["**Teaching points**", *_cited_list(bullets), ""]
        if concise:
            if c["interview_questions"]:
                L += ["**Interview questions**"]
                for q in c["interview_questions"]:
                    L += [f"- **Q:** {q['question']}", f"  **A (30-60 s):** {q['short_answer']}"]
                L += [""]
            continue
        if c["definitions"]:
            L += ["**Definitions**", *[f"- **{d['term']}** - {d['definition']}" + (f" *[{PROV_LABEL[d['provenance']]}]*" if d['provenance'] != 'taught' else '') + _refs(d.get('refs', [])) for d in c["definitions"]], ""]
        if c["why_it_matters"] or c["when_used"]:
            L += ["**Why it matters / when it is used.** " + " ".join(x for x in [c["why_it_matters"], c["when_used"]] if x), ""]
        if c["analogy"]:
            L += ["**Analogy.** " + c["analogy"], "", "*Where the analogy breaks down:* " + (c["analogy_limits"] or "—"), ""]
        if c["steps"]:
            L += ["**Step by step**", *[f"{i}. {s}" for i, s in enumerate(c["steps"], start=1)], ""]
        for ex in c["examples"]:
            tag = f" *[{PROV_LABEL[ex['provenance']]}]*" if ex["provenance"] != "taught" else ""
            L += [f"**Example: {ex['title']}**{tag}{_refs(ex.get('refs', []))}", "", ex["body"], ""]
            if ex.get("code"):
                L += [f"```{ex.get('language') or ''}", ex["code"], "```", ""]
            for iid in ex.get("image_ids", []):
                im = next((i for i in notes.get("images", []) if i["id"] == iid), None)
                if im:
                    L += [f"![{im.get('caption', iid)}]({image_base}/{Path(im['path']).name})", "", f"*{im['id']}: {im.get('caption', '')}*", ""]
        pf = c["pitfalls"]
        for key, label in (("assumptions", "Assumptions"), ("tradeoffs", "Tradeoffs"), ("limitations", "Limitations"), ("common_mistakes", "Common mistakes")):
            if pf.get(key):
                L += [f"**{label}**", *_cited_list(pf[key]), ""]
        if c["connections"]:
            L += ["**Connections to earlier concepts.** " + c["connections"], ""]
        if c["deeper_explanation"]:
            L += ["**Deeper explanation (advanced).** " + c["deeper_explanation"], ""]
        if c["fde_applications"]:
            L += ["**Forward deployed engineering applications**", *_cited_list(c["fde_applications"]), ""]
        if c["moments"]:
            L += ["**Demonstrations, troubleshooting, corrections, Q&A**",
                  *[f"- *{m['kind']}*: {m['summary']}" + (f" {m['detail']}" if m.get('detail') else "") + _refs(m.get("refs", [])) for m in c["moments"]], ""]
        if c["brief_mentions"]:
            L += ["**Brief mentions grouped here**", *_cited_list(c["brief_mentions"]), ""]
        for iid in c.get("image_ids", []):
            im = next((i for i in notes.get("images", []) if i["id"] == iid), None)
            if im:
                L += [f"![{im.get('caption', iid)}]({image_base}/{Path(im['path']).name})", "", f"*{im['id']}: {im.get('caption', '')}*", ""]
        if c["interview_questions"]:
            L += ["**Interview preparation**"]
            for q in c["interview_questions"]:
                src = "instructor's answer" if q["source"] == "instructor" else "generated practice answer"
                L += [f"- **Q: {q['question']}** *({src})*", f"  - **Answer (30-60 s):** {q['short_answer']}", f"  - **Deeper answer:** {q['deep_answer']}"]
                for fu in q["follow_ups"]:
                    L += [f"  - *Follow-up:* {fu['question']} - {fu['suggested_answer']}"]
                if q["conditions"]:
                    L += [f"  - *What changes the answer:* {q['conditions']}"]
            L += [""]
        if c["flags"]:
            L += ["**Flags**", *[f"- ⚠ {f['kind']}: {f['note']}{_refs(f.get('refs', []))}" for f in c["flags"]], ""]

    L += ["## C. Technical glossary", ""]
    for g in notes["glossary"]:
        L += [f"- **{g['term']}** - {g['explanation']} *(concept {g['concept_id'] or '—'}, {g['source']})*{_refs(g.get('refs', []))}"]
    L += [""]

    L += ["## D. Research papers mentioned", ""]
    lecture_papers = [p for p in notes["papers"] if not p.get("is_additional_reading")]
    extra_papers = [p for p in notes["papers"] if p.get("is_additional_reading")]
    if not lecture_papers:
        L += ["*No research papers were identified in the transcript.*", ""]
    for p in lecture_papers:
        conf = {"confident": "identified", "uncertain": "uncertain match", "unidentified": "not identified"}[p["confidence"]]
        line = f"- **“{p['title_as_heard']}”** (as heard){_refs(p.get('refs', []))} - {conf}"
        if p.get("identified_title"):
            line += f": *{p['identified_title']}*" + (f", {p['authors']}" if p.get("authors") else "") + (f" ({p['year']})" if p.get("year") else "")
        if p.get("link"):
            line += f" - {p['link']}" + (" ✓ verified" if p.get("link_verified") else " (unverified)")
        L += [line]
        if p.get("why_it_matters"):
            L += [f"  - Why it matters: {p['why_it_matters']}"]
    if extra_papers and not concise:
        L += ["", "### Additional reading (not mentioned in the lecture)"]
        for p in extra_papers:
            L += [f"- {p.get('identified_title') or p['title_as_heard']}" + (f" - {p['link']}" if p.get("link") else "") + (" ✓" if p.get("link_verified") else "")]
    L += [""]

    L += ["## E. Websites and resources mentioned", ""]
    lec_res = [r for r in notes["resources"] if not r.get("is_additional")]
    extra_res = [r for r in notes["resources"] if r.get("is_additional")]
    if not lec_res:
        L += ["*No websites or tools were identified in the transcript.*", ""]
    for r in lec_res:
        line = f"- **{r['name']}** ({r['kind']}){_refs(r.get('refs', []))} - {r['purpose']}"
        if r.get("url"):
            line += f" - {r['url']}" + (" ✓ verified" if r.get("url_verified") else " (unverified)")
        elif r.get("url_as_heard"):
            line += f" - heard as “{r['url_as_heard']}” (URL incomplete/uncertain)"
        if r.get("uncertain"):
            line += " ⚠ uncertain"
        L += [line]
    if extra_res and not concise:
        L += ["", "### Added recommendations (not mentioned in the lecture)"]
        for r in extra_res:
            L += [f"- {r['name']} - {r['purpose']}" + (f" - {r['url']}" if r.get("url") else "")]
    L += [""]

    sm = notes["summary"]
    L += ["## F. Summary and what comes next", "", "### Main lessons", *[f"- {x}" for x in sm["main_lessons"]], "",
          "### How they connect", sm["how_they_connect"], "", "### Next topic announced by the instructor",
          sm["next_topic_announced"] if sm["next_topic_was_announced"] else "*The instructor did not announce the next topic in this transcript.*", "",
          "### Suggested next study steps", *[f"- {x}" for x in sm["suggested_next_steps"]], ""]

    pr = notes["practice"]
    if not concise:
        L += ["## G. Practice", "", f"*{pr['progression_note']}*", ""]
        for lvl, label in (("basic", "Recall: basic understanding"), ("application", "Application"), ("interview", "Interview discussion")):
            qs = [q for q in pr["recall"] if q["level"] == lvl]
            if qs:
                L += [f"### {label}"]
                for i, q in enumerate(qs, start=1):
                    L += [f"{i}. **{q['question']}**", f"   <details><summary>Answer</summary>{q['answer']}</details>", ""]
        for ex in pr["exercises"]:
            L += [f"### Exercise: {ex['title']}", ex["task"], ""]
            if ex["hints"]:
                L += ["Hints:", *[f"- {h}" for h in ex["hints"]], ""]
            if ex["solution_outline"]:
                L += [f"<details><summary>Solution outline</summary>{ex['solution_outline']}</details>", ""]

    cov = notes.get("coverage", {})
    L += ["## Coverage report", "",
          f"- Transcript paragraphs: {cov.get('segments_total', 0)}; referenced by notes: {cov.get('segments_referenced', 0)}; chunks processed: {cov.get('chunk_count', 0)}",
          f"- Beginning covered: {'yes' if cov.get('beginning_covered') else 'no'}; middle: {'yes' if cov.get('middle_covered') else 'no'}; end: {'yes' if cov.get('end_covered') else 'no'}; "
          f"demonstrations: {'yes' if cov.get('demonstrations_covered') else 'no'}; Q&A: {'yes' if cov.get('qa_covered') else 'no'}"]
    for g in cov.get("gaps", []):
        L += [f"- Gap ({g['severity']}): {g['description']}"]
    for n in cov.get("notes", []):
        L += [f"- {n}"]
    L += [""]

    if not concise and transcript:
        L += ["## H. Source material: transcript", ""]
        for s in transcript.get("segments", []):
            tag = s["pid"] + (f" @{format_timestamp(s['start'])}" if s.get("start") is not None else "")
            spk = f"**{s['speaker']}:** " if s.get("speaker") else ""
            L += [f"<a id=\"{s['pid']}\"></a>`{tag}` {spk}{s['text']}", ""]
        if notes.get("images"):
            L += ["## H. Source material: screenshots and slides", ""]
            for im in notes["images"]:
                ts = f" @{format_timestamp(im['timestamp'])}" if im.get("timestamp") is not None else ""
                L += [f"![{im.get('caption', '')}]({image_base}/{Path(im['path']).name})", "",
                      f"*{im['id']} - {im.get('source_file', '')}{(' p.' + str(im['page'])) if im.get('page') else ''}{ts}: {im.get('caption', '')}*", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ html

HTML_CSS = """
:root{--ink:#1d1d1f;--muted:#6e6e73;--rule:#e5e5ea;--accent:#2f5fd0;--taught:#1b7f4e;--add:#8a5a00;--ver:#b3261e;--bg:#fff}
*{box-sizing:border-box}body{font-family:-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);max-width:860px;margin:0 auto;padding:32px 24px;line-height:1.55;font-size:11.5pt}
h1{font-size:26pt;margin:0 0 6px}h2{font-size:17pt;margin:34px 0 10px;border-bottom:2px solid var(--rule);padding-bottom:6px;page-break-after:avoid}
h3{font-size:13.5pt;margin:24px 0 8px;page-break-after:avoid}h4{margin:14px 0 6px}
p{margin:6px 0}ul,ol{margin:4px 0 8px;padding-left:22px}li{margin:3px 0}
code{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:9.5pt;background:#f4f4f6;padding:1px 4px;border-radius:3px}
pre{background:#f4f4f6;padding:10px 12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;orphans:3;widows:3}
pre code{background:none;padding:0}blockquote{border-left:4px solid var(--accent);margin:10px 0;padding:6px 12px;background:#f6f8ff;color:#333}
img{max-width:100%;height:auto;border:1px solid var(--rule);border-radius:4px;page-break-inside:avoid}
em{color:var(--muted)}table{border-collapse:collapse;width:100%}td,th{border:1px solid var(--rule);padding:4px 8px;text-align:left}
details{margin:4px 0 8px}summary{cursor:pointer;color:var(--accent)}
.legend{color:var(--muted);font-size:10pt}
@media print{body{padding:0;font-size:10.5pt}h2{page-break-before:auto}details{display:block}details>summary{display:none}details>*{display:block}
 a{color:inherit;text-decoration:none} pre{font-size:8.5pt}}
"""


def _embed_images(md_html: str, ldir: Path) -> str:
    def repl(m: re.Match) -> str:
        src = html.unescape(m.group(1))
        p = ldir / "images" / Path(src).name
        if p.exists():
            mime = mimetypes.guess_type(p.name)[0] or "image/png"
            data = base64.b64encode(p.read_bytes()).decode()
            return f'src="data:{mime};base64,{data}"'
        return m.group(0)
    return re.sub(r'src="([^"]+)"', repl, md_html)


def render_html(lecture: dict[str, Any], notes: dict[str, Any], mode: str = "detailed",
                transcript: dict[str, Any] | None = None, embed: bool = True) -> str:
    md = render_markdown(lecture, notes, mode, transcript)
    body = MarkdownIt("commonmark", {"html": True}).enable("table").render(md)
    if embed:
        body = _embed_images(body, lecture_dir(lecture["id"]))
    title = html.escape(lecture["title"])
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title><style>{HTML_CSS}</style></head><body>{body}</body></html>"


# ------------------------------------------------------------------ pdf

def export_pdf(lecture: dict[str, Any], notes: dict[str, Any], mode: str, transcript: dict[str, Any] | None, out_path: Path) -> Path:
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("No Chrome/Chromium found for PDF export. Install Google Chrome or export HTML and print it.")
    html_text = render_html(lecture, notes, mode, transcript, embed=True)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "notes.html"
        src.write_text(html_text, encoding="utf-8")
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
               f"--print-to-pdf={out_path}", f"--user-data-dir={td}/profile", "--no-first-run", src.as_uri()]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if not out_path.exists():
            raise RuntimeError(f"Chrome PDF export failed: {r.stderr[-500:]}")
    return out_path


# ------------------------------------------------------------------ docx

def export_docx(lecture: dict[str, Any], notes: dict[str, Any], mode: str, transcript: dict[str, Any] | None, out_path: Path) -> Path:
    import docx
    from docx.enum.text import WD_BREAK
    from docx.shared import Inches, Pt, RGBColor

    ldir = lecture_dir(lecture["id"])
    d = docx.Document()
    st = d.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)
    for s in d.sections:
        s.left_margin = s.right_margin = Inches(0.9)
        s.top_margin = s.bottom_margin = Inches(0.8)

    def para(text: str = "", style: str | None = None, italic=False, bold=False, color: RGBColor | None = None, size: int | None = None):
        p = d.add_paragraph(style=style) if style else d.add_paragraph()
        if text:
            run = p.add_run(text)
            run.italic, run.bold = italic, bold
            if color:
                run.font.color.rgb = color
            if size:
                run.font.size = Pt(size)
        return p

    def rich(p, text: str):
        """Minimal **bold** / *italic* / `code` inline markup."""
        for tok in re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)", text):
            if not tok:
                continue
            if tok.startswith("**"):
                p.add_run(tok[2:-2]).bold = True
            elif tok.startswith("*"):
                p.add_run(tok[1:-1]).italic = True
            elif tok.startswith("`"):
                r = p.add_run(tok[1:-1]); r.font.name = "Menlo"; r.font.size = Pt(9)
            else:
                p.add_run(tok)

    def bullets(items: list[str], style="List Bullet"):
        for it in items:
            rich(d.add_paragraph(style=style), it)

    def code_block(code: str):
        for line in code.splitlines() or [""]:
            p = d.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            r = p.add_run(line)
            r.font.name = "Menlo"
            r.font.size = Pt(8.5)
        d.add_paragraph()

    def image(im: dict[str, Any]):
        p = ldir / im["path"]
        if p.exists():
            try:
                d.add_picture(str(p), width=Inches(5.5))
                para(f"{im['id']} - {im.get('caption', '')}", italic=True, size=9)
            except Exception:
                para(f"[image {im['id']} could not be embedded]", italic=True)

    GREY = RGBColor(0x6E, 0x6E, 0x73)
    md = render_markdown(lecture, notes, mode, transcript, image_base="images")
    # Walk the markdown line by line so DOCX mirrors the other exports exactly.
    in_code, code_buf, code_lang = False, [], ""
    ol_counter = 0
    for raw in md.splitlines():
        line = raw.rstrip()
        if in_code:
            if line.startswith("```"):
                code_block("\n".join(code_buf)); in_code, code_buf = False, []
            else:
                code_buf.append(raw)
            continue
        if line.startswith("```"):
            in_code, code_lang = True, line[3:]
            continue
        if not line.strip():
            continue
        if line.startswith("# "):
            d.add_heading(line[2:], 0)
        elif line.startswith("## "):
            d.add_heading(line[3:], 1)
        elif line.startswith("### "):
            d.add_heading(line[4:], 2)
        elif line.startswith("![") :
            m = re.match(r"!\[.*\]\(([^)]+)\)$", line)
            if m:
                name = Path(m.group(1)).name
                im = next((i for i in notes.get("images", []) if Path(i["path"]).name == name), None)
                if im:
                    image(im)
        elif line.startswith("> "):
            p = para(); rich(p, line[2:]); p.paragraph_format.left_indent = Inches(0.3)
            for r in p.runs:
                r.font.color.rgb = GREY
        elif re.match(r"^\s*- ", line):
            indent = (len(line) - len(line.lstrip())) // 2
            p = d.add_paragraph(style="List Bullet 2" if indent else "List Bullet")
            rich(p, re.sub(r"^\s*- ", "", line))
        elif re.match(r"^\d+\. ", line):
            p = d.add_paragraph(style="List Number"); rich(p, re.sub(r"^\d+\. ", "", line))
        elif line.startswith("<details>"):
            inner = re.sub(r"<[^>]+>", " ", line).strip()
            p = para(); rich(p, inner)
            for r in p.runs:
                r.font.color.rgb = GREY
        elif line.startswith("<a id="):
            p = para(); rich(p, re.sub(r"<[^>]+>", "", line))
        elif line.startswith("*") and line.endswith("*") and not line.startswith("**"):
            para(line.strip("*"), italic=True, color=GREY)
        else:
            p = para(); rich(p, re.sub(r"<[^>]+>", "", line))
    d.save(str(out_path))
    return out_path


def export_all(lecture: dict[str, Any], notes: dict[str, Any], transcript: dict[str, Any] | None, out_dir: Path, mode: str) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\- ]+", "", lecture["title"]).strip().replace(" ", "_")[:60] or "lecture"
    stem = f"{stem}_{mode}"
    paths: dict[str, Path] = {}
    (out_dir / f"{stem}.md").write_text(render_markdown(lecture, notes, mode, transcript, image_base="../images"), encoding="utf-8")
    paths["md"] = out_dir / f"{stem}.md"
    (out_dir / f"{stem}.html").write_text(render_html(lecture, notes, mode, transcript), encoding="utf-8")
    paths["html"] = out_dir / f"{stem}.html"
    paths["docx"] = export_docx(lecture, notes, mode, transcript, out_dir / f"{stem}.docx")
    try:
        paths["pdf"] = export_pdf(lecture, notes, mode, transcript, out_dir / f"{stem}.pdf")
    except RuntimeError as e:
        paths["pdf_error"] = e  # type: ignore[assignment]
    return paths
