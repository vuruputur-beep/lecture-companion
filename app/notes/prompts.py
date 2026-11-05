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
8. Write for a complete beginner who has never heard of tokens, embeddings, vectors, parameters, APIs or models. Style: short, direct, declarative sentences, like a good textbook. The first time a technical term appears in a concept, explain it in a sentence of its own that says what it is and then makes it concrete with a contrast or an effect, for example: "It is probabilistic: instead of one fixed answer, it assigns probabilities to many possible next words and generates text token by token." or "Transformers process input text, after it is broken into tokens and converted to vectors called embeddings, using attention layers that focus on the most relevant parts of the context." Do not use parenthetical glosses, "imagine…" or "think of…" openers, or chatty framing; put analogies only in the analogy field. Explain why before how, and say how each idea connects to the previous one so the reader always knows where they are.
9. Spelling and names: the transcript is machine-generated and contains garbled words. In your own prose always use the correct, standard spelling (e.g. "Qwen", "multi-head attention", "feed-forward network", "Anthropic"); record what was actually heard only inside a `flags` entry such as "heard as 'Quen'". Never copy a garbled phrase into bullets, definitions, titles or glossary terms. Proofread your output for typos, doubled words and broken sentences before returning it.
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
- in_one_breath: one or two plain sentences with zero jargon saying what this part was about.
- plain_explanation assuming no prior knowledge: direct textbook prose; each new term explained in its own sentence the first time it appears (what it is, then a concrete contrast or effect); end by saying how this connects to what came before.
- teaching_bullets: at least 10 meaningful bullets for a substantive concept (fewer only if the lecture genuinely has less; never pad). For housekeeping/logistics or a non-substantive concept, 2-5 bullets, no analogy, and no interview questions. Each bullet is one teaching point, provenance-labelled, with refs for taught points.
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
- overview.foundations: 6-15 background ideas the lecture assumes but never explains (for example: what a model is, what a token is, what a vector/embedding is, what "parameters" are, what an API is, what training vs inference means - whichever actually apply to this lecture). Each gets a 2-4 sentence everyday-words explanation, an everyday analogy, and where in the lecture it is assumed. Mark nothing here as taught; it is background.
- overview.big_picture: one_paragraph_story (the whole lecture as a short story a beginner can follow), flow (the end-to-end flow as 5-12 ordered steps, each naming the concept number it belongs to, e.g. "Step 3 (Concept 4): ..."), mental_model (one memorable picture for the whole lecture and where it breaks down), where_this_fits (how this lecture relates to the course and to an FDE's day-to-day work).
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

GUIDE_USER = """TASK: Build one combined study guide from these {n} lectures of the course "{course}". The reader is a complete beginner who has read none of them yet.

Use only the lecture notes supplied below (they were generated from the transcripts and are the ground truth here). Do not invent lecture content.

Produce:
- title, and a story of how the lectures build on each other.
- themes: 4-10 cross-lecture themes; each lists the concepts that belong to it (by lecture_id + concept_id + title, exactly as given) and explains how they connect.
- foundations: merge the lectures' foundation entries, deduplicated, keeping the clearest explanation.
- learning_path: ordered study sessions of about 20 minutes each (the reader has ADHD and studies in short sessions), each pointing at specific lecture/concept ids.
- interview_bank: group the EXISTING interview questions by theme using references only (lecture_id, concept_id, index as listed) - do not rewrite questions.
- overlaps: ideas taught in more than one lecture and how the treatments differ; gaps: things assumed but never taught across the set.
- glossary: merged and deduplicated; put the lecture_id(s) in refs.

LECTURES:
{digest}"""

CONCEPT_PART_USER = """TASK: Regenerate ONLY these parts of the study-notes entry for Concept {number}: "{title}" (lecture "{lecture_title}"): {parts}.
Keep everything consistent with the rest of the entry (shown for context) and follow the same rules: grounded in the transcript excerpt, provenance labels and refs, correct spellings, beginner-friendly direct prose. Return only the requested parts.

CURRENT VERSION OF THESE PARTS (improve on it; do not just copy):
{current}

CONTEXT FROM THE REST OF THE ENTRY:
{context}
{visual_context}
TRANSCRIPT EXCERPT:
{excerpt}"""

EXPLAIN_MODES = {
    "explain": "Explain this passage to a complete beginner. Say what it means in plain words, define any term in it, and say why it matters here.",
    "simpler": "Rewrite this passage in simpler language for a complete beginner, keeping the meaning exact. Short sentences, no jargon without a plain-words explanation.",
    "example": "Give one concrete, worked example that makes this passage click for a beginner, then one sentence on what the example shows.",
    "why": "Explain why this is true or why it matters, step by step, for a beginner. If it depends on assumptions, say which.",
}

EXPLAIN_USER = """A student reading their lecture notes selected this passage and asked for help.

MODE: {mode_instruction}

SELECTED PASSAGE:
\"\"\"{quote}\"\"\"

WHERE IT APPEARS (section/concept context from the notes):
{context}

RELEVANT GLOSSARY ENTRIES:
{glossary}

Rules: stay faithful to the lecture notes and transcript context above; if the notes are ambiguous, say so instead of guessing. Use the direct, declarative style (short sentences; each new term explained in its own sentence with a concrete contrast). 80-200 words unless an example needs a short code block. Do not append provenance labels or citation lists; if you cite, do it inline like (P12). Return JSON with a single field "explanation" containing Markdown text."""
