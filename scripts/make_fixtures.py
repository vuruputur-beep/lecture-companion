"""Generate clearly-labelled synthetic fixtures (Otter-style TXT, SRT, DOCX, PDF, summary)
so the import → notes → export path can be verified without real lecture data.
Run: .venv/bin/python scripts/make_fixtures.py
"""
from __future__ import annotations

import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)
random.seed(7)

TOPICS = [
    ("retrieval augmented generation", "RAG lets a model look things up before it answers. You embed documents into vectors, store them in a vector database, and at query time you retrieve the top k chunks and put them in the prompt."),
    ("chunking strategies", "If chunks are too big you blow the context window; too small and you lose meaning. Many teams start with five hundred tokens and a fifty token overlap, then tune against an eval set."),
    ("evaluation", "You cannot improve what you do not measure. Build a golden set of questions and expected answers, then score retrieval recall and answer faithfulness separately."),
    ("the forward deployed engineer role", "An FDE sits with the customer, understands their data and workflow, and ships a working integration in weeks. You are half engineer, half consultant."),
    ("prompt caching", "Prompt caching reuses a stable prefix so repeated calls are cheaper. Put the system prompt and the big documents first, the question last."),
    ("tool use and agents", "The model emits a structured tool call, your code runs it, and you return the result. Loop until the model stops asking for tools. Always validate the arguments."),
    ("deployment and observability", "Log every request with its token counts and latency. Trace retrieval separately from generation so you can tell which half failed."),
]
FILLER = [
    "Let me show you on the screen here.", "Does that make sense to everyone?", "So the key idea is this.",
    "In the paper Attention Is All You Need they introduced the transformer, which is the architecture under all of this.",
    "You can find the models on Hugging Face, huggingface dot co.", "We will use the LangChain docs as a reference, but you do not need the framework.",
    "I will put the notebook in the Slack channel after class.", "Let me fix that typo, it should be top underscore k not top k.",
    "Okay, the error says connection refused, that is because the vector database container is not running, let me start it.",
]
QA = [
    ("Speaker 2", "Quick question, how do you pick the embedding model?"),
    ("Speaker 1", "Good question. Start with a general model, measure recall on your golden set, and only switch if the numbers tell you to."),
    ("Speaker 3", "What if the customer's documents are scanned PDFs?"),
    ("Speaker 1", "Then you need OCR before chunking, and you should expect noisier retrieval. Flag that as a risk early in the engagement."),
]


def ts(sec: int) -> str:
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_segments(n_topics: int = 7, paras_per_topic: int = 12) -> list[tuple[str, int, str]]:
    segs = []
    t = 0
    for ti, (name, core) in enumerate(TOPICS[:n_topics]):
        segs.append(("Speaker 1", t, f"Okay, let's move on to {name}. {core}"))
        t += random.randint(40, 90)
        for p in range(paras_per_topic):
            sent = [core, random.choice(FILLER), f"Think of {name} as {random.choice(['a library index', 'a checklist', 'a translator', 'a map'])}, that is the analogy I like.",
                    f"A common mistake with {name} is to skip measurement and trust vibes.", random.choice(FILLER)]
            random.shuffle(sent)
            segs.append(("Speaker 1", t, " ".join(sent)))
            t += random.randint(45, 120)
        if ti % 2 == 1:
            for spk, line in QA:
                segs.append((spk, t, line))
                t += random.randint(15, 40)
    segs.append(("Speaker 1", t, "Next week we will cover evaluation harnesses in depth and start the capstone project. See you Sunday."))
    return segs


def write_otter_txt(segs, path: Path, title="FDE Week 4 - RAG Systems (SYNTHETIC FIXTURE)"):
    lines = [title, "Sun, Oct 26, 2025 9:02AM", ""]
    for spk, t, text in segs:
        lines += [f"{spk}  {ts(t)}", text, ""]
    path.write_text("\n".join(lines))


def write_srt(segs, path: Path):
    out = []
    for i, (spk, t, text) in enumerate(segs, start=1):
        end = t + 30
        out.append(f"{i}\n{ts(t).rjust(7, '0') if t >= 3600 else '00:' + ts(t).rjust(5, '0')},000 --> "
                   f"{ts(end).rjust(7, '0') if end >= 3600 else '00:' + ts(end).rjust(5, '0')},000\n{spk}: {text}\n")
    path.write_text("\n".join(out))


def write_untimed_txt(segs, path: Path):
    path.write_text("FDE Week 3 - Evaluation (SYNTHETIC FIXTURE, no timestamps)\n\n" + "\n\n".join(text for _, _, text in segs))


def write_summary_txt(path: Path):
    path.write_text("""FDE Week 2 - Tool Use (SYNTHETIC FIXTURE Otter summary)
Sun, Oct 12, 2025 9:00AM

Summary
The instructor introduced tool use and agent loops, showed a demo of a weather tool, and discussed validation of arguments. Students asked about error handling.

Outline
- Tool definitions and schemas
- The agent loop
- Validation and retries

Action Items
- Read the tool use documentation
- Finish exercise 2
""")


def write_docx(segs, path: Path, image_png: bytes):
    import docx
    from docx.shared import Inches
    d = docx.Document()
    d.add_paragraph("FDE Week 4 - RAG Systems (SYNTHETIC FIXTURE DOCX)")
    d.add_paragraph("Sun, Oct 26, 2025 9:02AM")
    for i, (spk, t, text) in enumerate(segs):
        d.add_paragraph(f"{spk}  {ts(t)}")
        d.add_paragraph(text)
        if i == 5:
            img = path.parent / "_tmp_img.png"
            img.write_bytes(image_png)
            d.add_picture(str(img), width=Inches(3))
            img.unlink()
    d.save(str(path))


def write_pdf(segs, path: Path, image_png: bytes):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 50
    page.insert_text((50, y), "FDE Week 4 - RAG Systems (SYNTHETIC FIXTURE PDF)", fontsize=12)
    y += 30
    for i, (spk, t, text) in enumerate(segs):
        for line in [f"{spk}  {ts(t)}"] + [text[j:j + 95] for j in range(0, len(text), 95)]:
            if y > 780:
                page = doc.new_page()
                y = 50
            page.insert_text((50, y), line, fontsize=8)
            y += 11
        y += 6
        if i == 3:
            page.insert_image(fitz.Rect(300, y, 500, y + 120), stream=image_png)
            y += 130
    doc.save(str(path))


def write_slides_pdf(path: Path):
    import fitz
    doc = fitz.open()
    for i, (name, core) in enumerate(TOPICS[:5], start=1):
        page = doc.new_page(width=720, height=405)
        page.insert_text((40, 60), f"Slide {i}: {name.title()}", fontsize=24)
        page.insert_text((40, 120), core[:90], fontsize=12)
        page.insert_text((40, 160), "def retrieve(q, k=5):\n    return index.search(embed(q), k)", fontsize=11)
    doc.save(str(path))


def make_png() -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 240), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, 390, 230], outline="black")
    d.text((20, 20), "SYNTHETIC FIXTURE SCREENSHOT", fill="black")
    d.text((20, 60), "top_k = 5", fill="black")
    d.text((20, 80), "chunk_size = 500", fill="black")
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


if __name__ == "__main__":
    long_segs = build_segments(7, 14)      # ~ 100 paragraphs, 4 hours
    short_segs = build_segments(3, 6)
    png = make_png()
    write_otter_txt(long_segs, OUT / "week4_rag_otter.txt")
    write_srt(long_segs, OUT / "week4_rag.srt")
    write_untimed_txt(short_segs, OUT / "week3_eval_untimed.txt")
    write_summary_txt(OUT / "week2_tooluse_summary.txt")
    write_docx(short_segs, OUT / "week4_rag.docx", png)
    write_pdf(short_segs, OUT / "week4_rag.pdf", png)
    write_slides_pdf(OUT / "week4_slides.pdf")
    (OUT / "screenshot_fixture.png").write_bytes(png)
    words = sum(len(t.split()) for _, _, t in long_segs)
    print(f"fixtures written to {OUT}; long transcript ≈ {words} words, {len(long_segs)} paragraphs, {ts(long_segs[-1][1])} long")
