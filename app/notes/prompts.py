"""Prompts for the notes pipeline. Kept in one place so they can be tuned."""

SYSTEM = """You turn course-lecture transcripts into detailed study notes and interview preparation for one student.

Context about the student: attending Interview Kickstart's Forward Deployed Engineer (FDE) course; has ADHD and finds long lectures hard to follow, so notes must be complete, well-structured, and beginner-friendly, so the lecture never has to be re-watched.

Source material: a transcript whose paragraphs carry citation ids like [P12] or [P12 @1:02:03] (paragraph id, and a timestamp when the export had one), plus optional visual context (slides/screenshots with ids like IMG3). Speaker labels, when present, are the export's labels; the instructor is usually the dominant speaker.

Non-negotiable rules:
1. Ground everything in the supplied material. Never invent lecture content, quotes, numbers, titles, authors, years, URLs, or timings. If a timestamp is not in the source, cite only the paragraph id.
2. Label provenance on every claim: "taught" = said or shown in the lecture (always include refs); "additional" = your own supplemental teaching explanation, clearly separate from what was taught; "needs_verification" = unclear speech, a likely transcription error, illegible code, a fact you cannot confirm, or a title/URL you are not sure about.
3. Cite refs as the ids exactly as given, e.g. "P12" or "P12@1:02:03". Prefer the paragraph ids where the statement actually appears.
4. A transcript cannot recover diagrams or code that only appeared on screen. Say so where it matters instead of guessing. When transcribed code or terms look garbled, give your best reading and flag it.
5. Do not pad. Bullets must each teach something real. If a concept is thin in the lecture, say it is thin rather than inflating it; supplemental explanations are welcome but must be labelled "additional".
6. Preserve the order of the lecture. Preserve demonstrations, troubleshooting, instructor corrections, and useful student Q&A.
7. Interview answers you write are generated practice answers unless the instructor actually answered the question in the lecture; mark `source` accordingly.
8. Write for a smart beginner: plain English first, precise terminology second, depth last.
Return only JSON that matches the requested schema."""

INVENTORY_USER = """TASK: Build a content inventory for chunk {chunk_no} of {chunk_total} of the lecture "{title}" ({course}, week {week}).
{prior_context}
{visual_context}
List, in order, every substantive thing that happens in this chunk: concepts taught, demonstrations, Q&A exchanges, troubleshooting, instructor corrections, administrative remarks, and brief mentions. For each item give the first and last paragraph ids it spans (start_ref/end_ref, e.g. "P14"). Record technical terms and acronyms with where they first appear, any research papers mentioned (exact wording heard), any websites/tools/repos/datasets mentioned (wording and any URL text), and quality flags (unclear speech, likely transcription errors, code that was only shown on screen, etc.). If the instructor says what comes next week / next session, capture it. Write a 3-6 sentence chunk_summary that lets the next chunk be understood in context.

TRANSCRIPT CHUNK (paragraph ids in brackets):
{chunk_text}"""

OUTLINE_USER = """TASK: Merge these per-chunk inventories of the lecture "{title}" into a single ordered concept outline.

Rules:
- Keep lecture order. Each outline entry becomes "Concept N: title" in the notes.
- Each substantive teaching topic is its own concept. A concept may span several chunks; give its first start_ref and last end_ref and list other places it is revisited in extra_refs.
- Fold brief mentions and administrative remarks into the most related concept (grouped_mentions) rather than making tiny concepts. Demonstrations, Q&A, and troubleshooting belong to the concept they illustrate.
- Aim for the natural number of concepts for this lecture (often 6-18 for a 4-hour class); do not split one idea into many or merge unrelated ideas.
- Mark is_substantive=false only for housekeeping-only sections (breaks, logistics) that still deserve a short entry.
- Ids: c1, c2, ... in order.

SOURCE ASSESSMENT: {assessment}

INVENTORIES:
{inventories}"""

CONCEPT_USER = """TASK: Write the full study-notes entry for Concept {number}: "{title}" from the lecture "{lecture_title}" ({course}, week {week}).
Scope: {scope}
Primary refs: {start_ref} to {end_ref}{extra}
Earlier concepts in this lecture (for the "connections" field): {earlier}
Grouped brief mentions to fold in: {mentions}
{visual_context}
Produce every field of the schema with real content:
- plain_explanation assuming no prior knowledge.
- teaching_bullets: at least 10 meaningful bullets for a substantive concept (fewer only if the lecture genuinely has less; never pad). Each bullet is one teaching point, provenance-labelled, with refs for taught points.
- definitions for every unfamiliar term used here.
- why_it_matters, when_used, analogy plus analogy_limits, steps (step-by-step), examples (worked examples; include code/calculations the instructor showed - if code was only on screen and is not in the transcript, say so in a flag rather than inventing it; you may add a clearly 'additional' illustrative example), pitfalls (assumptions, tradeoffs, limitations, common mistakes), connections, deeper_explanation, fde_applications (how this applies to forward deployed engineering work with customers).
- moments: demonstrations, troubleshooting, instructor corrections, useful student Q&A that occurred in this span, with refs.
- interview_questions: 2-5 likely questions for a major concept, each with a polished 30-60 second spoken answer (short_answer), a deeper answer, likely follow-ups with suggested answers, and the conditions/tradeoffs that change the answer. source="instructor" only if the instructor actually answered it in the lecture.
- flags for unclear speech, transcription errors, illegible code, missing visual context.
- time_range: use the timestamps in the refs if present ("0:12:04 - 0:41:30"), otherwise the paragraph range ("P4 - P19").
- id must be "{cid}", number {number}, is_substantive {is_substantive}.

TRANSCRIPT EXCERPT (this concept's span, with a little surrounding context):
{excerpt}"""

WRAPUP_USER = """TASK: Write the lecture-level sections for "{title}" ({course}, week {week}).

SOURCE ASSESSMENT: {assessment}
{insufficiency}
CONCEPTS (in order) WITH THEIR KEY POINTS:
{concept_digest}

CHUNK SUMMARIES (whole lecture in order):
{chunk_summaries}

NEXT-TOPIC ANNOUNCEMENTS FOUND IN TRANSCRIPT (verbatim-ish; empty if none): {next_topic}

TERMS MENTIONED (term -> first ref): {terms}

Produce:
- overview: learning_objectives, prerequisites, a short beginner-friendly introduction, and at least 10 meaningful takeaways (provenance-labelled with refs).
- summary: main_lessons and how_they_connect; next_topic_announced only from the announcements above (set next_topic_was_announced=false and leave it empty if the instructor did not announce one); suggested_next_steps as your own study suggestions.
- practice: recall questions (answers hidden in the UI) spanning levels basic -> application -> interview, each tied to a concept_id; at least one practical exercise with hints and a solution outline; a progression_note describing how to work through them.
- glossary: every technical term/acronym mentioned in speech or shown in visuals, explained briefly in simple English, linked to concept_id, with source speech/visual/both.
- insufficiency_note: if the material is a summary rather than a full transcript, or is very short, explain plainly what the notes could and could not cover. Empty otherwise.
- global_flags: lecture-wide quality issues."""

MENTIONS_USER = """TASK: Consolidate the research papers and the websites/tools/repositories/datasets mentioned across the lecture "{title}".

Rules: use only the wording in these mentions; do NOT invent titles, authors, years, or URLs. Deduplicate repeated mentions. For each paper give title_as_heard (exact wording), why_it_matters (from the lecture context), refs, and leave identified_title/authors/year/link EMPTY - verification happens separately. For each resource give the name, kind, url_as_heard (only if a URL or domain was actually spoken/shown), purpose, refs; set uncertain=true when the name or URL is garbled or incomplete. Set is_additional_reading/is_additional = false for everything here (these are lecture references). Then, separately, you may add up to 5 additional-reading papers and up to 5 additional resources you are highly confident exist and are directly relevant, marked is_additional_reading/is_additional = true, with link left empty unless you are certain of the canonical URL.

PAPER MENTIONS:
{papers}

RESOURCE MENTIONS:
{resources}"""

IMAGE_USER = """Describe this image from a lecture ("{title}"). Return: a one-sentence caption; all legible text verbatim in `ocr_text` (code, slide text, terminal output, labels) preserving line breaks; `kind` as slide, code, terminal, diagram, whiteboard, screenshot, photo, or other; and `legibility` as clear, partial, or illegible. Do not guess at text you cannot read."""
