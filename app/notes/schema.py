"""The single structured notes model.

Everything the UI shows and every export (Markdown/HTML/PDF/DOCX) is rendered
from `LectureNotes`. Every claim carries provenance:
  taught              - stated in the lecture (transcript/visuals), with refs
  additional          - supplemental teaching explanation added by the model
  needs_verification  - unclear speech, possible transcription error, or an
                        unverified fact (paper title, URL, number)
`refs` are citation ids from the transcript: "P12" (paragraph) or "P12@1:02:03".
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Provenance = Literal["taught", "additional", "needs_verification"]


class Cited(BaseModel):
    text: str
    provenance: Provenance = "taught"
    refs: list[str] = Field(default_factory=list)


class Definition(BaseModel):
    term: str
    definition: str
    provenance: Provenance = "taught"
    refs: list[str] = Field(default_factory=list)


class Example(BaseModel):
    title: str
    body: str = Field(description="Prose walkthrough of the example")
    code: str = Field(default="", description="Code, calculation, or pseudo-code; empty if none")
    language: str = Field(default="", description="Language hint for code, e.g. python, bash, text")
    provenance: Provenance = "taught"
    refs: list[str] = Field(default_factory=list)
    image_ids: list[str] = Field(default_factory=list, description="Ids of attached screenshots/slides that show this example")


class FollowUp(BaseModel):
    question: str
    suggested_answer: str


class InterviewQuestion(BaseModel):
    question: str
    short_answer: str = Field(description="Polished 30-60 second spoken answer")
    deep_answer: str = Field(description="Deeper answer for follow-up discussion")
    follow_ups: list[FollowUp] = Field(default_factory=list)
    conditions: str = Field(default="", description="Conditions or tradeoffs that change the answer")
    source: Literal["generated", "instructor"] = "generated"
    refs: list[str] = Field(default_factory=list)


class Moment(BaseModel):
    """Demonstrations, troubleshooting, instructor corrections, student Q&A."""
    kind: Literal["demonstration", "troubleshooting", "correction", "qa", "aside"]
    summary: str
    detail: str = ""
    refs: list[str] = Field(default_factory=list)


class Flag(BaseModel):
    kind: Literal["unclear_speech", "transcription_error", "illegible_code", "missing_visual", "uncertain_fact", "other"]
    note: str
    refs: list[str] = Field(default_factory=list)


class Pitfalls(BaseModel):
    assumptions: list[Cited] = Field(default_factory=list)
    tradeoffs: list[Cited] = Field(default_factory=list)
    limitations: list[Cited] = Field(default_factory=list)
    common_mistakes: list[Cited] = Field(default_factory=list)


class Concept(BaseModel):
    id: str = Field(description="Stable short id, e.g. c3")
    number: int
    title: str
    time_range: str = Field(default="", description="e.g. '0:12:04 - 0:41:30' or 'P4 - P19'")
    refs: list[str] = Field(default_factory=list, description="Primary transcript refs spanning this concept")
    in_one_breath: str = Field(default="", description="One or two sentences a non-technical friend would understand; zero jargon")
    plain_explanation: str = Field(description="Plain-English explanation assuming no prior knowledge; every technical term glossed in parentheses on first use")
    teaching_bullets: list[Cited] = Field(default_factory=list, description="At least 10 meaningful bullets for substantive concepts")
    definitions: list[Definition] = Field(default_factory=list)
    why_it_matters: str = ""
    when_used: str = ""
    analogy: str = ""
    analogy_limits: str = ""
    steps: list[str] = Field(default_factory=list, description="Step-by-step explanation")
    examples: list[Example] = Field(default_factory=list)
    pitfalls: Pitfalls = Field(default_factory=Pitfalls)
    connections: str = Field(default="", description="Connections to earlier concepts in this lecture or course")
    deeper_explanation: str = ""
    fde_applications: list[Cited] = Field(default_factory=list, description="Applications to forward deployed engineering")
    moments: list[Moment] = Field(default_factory=list)
    brief_mentions: list[Cited] = Field(default_factory=list, description="Short related mentions grouped here")
    interview_questions: list[InterviewQuestion] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    is_substantive: bool = True
    image_ids: list[str] = Field(default_factory=list)
    user_edited_fields: list[str] = Field(default_factory=list)


class Foundation(BaseModel):
    """A background idea the lecture takes for granted, explained for a complete beginner."""
    term: str
    plain_explanation: str = Field(description="2-4 sentences in everyday words, no unexplained jargon")
    why_needed_here: str = Field(default="", description="Where in this lecture it is assumed")
    everyday_analogy: str = ""


class BigPicture(BaseModel):
    """How everything in the lecture fits together."""
    one_paragraph_story: str = Field(default="", description="The whole lecture as a short story for a beginner")
    flow: list[str] = Field(default_factory=list, description="The end-to-end flow as ordered steps, each naming the concept it belongs to")
    mental_model: str = Field(default="", description="One memorable picture or metaphor for the whole thing, with its limits")
    where_this_fits: str = Field(default="", description="How this lecture relates to the course / an FDE's job")


class Overview(BaseModel):
    learning_objectives: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    introduction: str = ""
    takeaways: list[Cited] = Field(default_factory=list, description="At least 10 meaningful takeaways")
    foundations: list[Foundation] = Field(default_factory=list, description="Background a beginner needs before this lecture makes sense")
    big_picture: BigPicture = Field(default_factory=BigPicture)
    user_edited_fields: list[str] = Field(default_factory=list)


class GlossaryEntry(BaseModel):
    term: str
    explanation: str
    concept_id: str = ""
    source: Literal["speech", "visual", "both"] = "speech"
    refs: list[str] = Field(default_factory=list)


class Paper(BaseModel):
    title_as_heard: str = Field(description="Exact wording from the transcript")
    identified_title: str = Field(default="", description="Verified title if confidently identified, else empty")
    authors: str = ""
    year: str = ""
    link: str = ""
    link_verified: bool = False
    confidence: Literal["confident", "uncertain", "unidentified"] = "unidentified"
    why_it_matters: str = ""
    refs: list[str] = Field(default_factory=list)
    is_additional_reading: bool = False


class Resource(BaseModel):
    name: str
    kind: Literal["website", "documentation", "repository", "tool", "dataset", "other"] = "other"
    url_as_heard: str = ""
    url: str = ""
    url_verified: bool = False
    uncertain: bool = False
    purpose: str = ""
    refs: list[str] = Field(default_factory=list)
    is_additional: bool = False


class Summary(BaseModel):
    main_lessons: list[str] = Field(default_factory=list)
    how_they_connect: str = ""
    next_topic_announced: str = Field(default="", description="What the instructor said comes next; empty if not announced")
    next_topic_was_announced: bool = False
    suggested_next_steps: list[str] = Field(default_factory=list)
    user_edited_fields: list[str] = Field(default_factory=list)


class RecallQuestion(BaseModel):
    question: str
    answer: str
    level: Literal["basic", "application", "interview"] = "basic"
    concept_id: str = ""


class Exercise(BaseModel):
    title: str
    task: str
    hints: list[str] = Field(default_factory=list)
    solution_outline: str = ""
    concept_ids: list[str] = Field(default_factory=list)


class Practice(BaseModel):
    recall: list[RecallQuestion] = Field(default_factory=list)
    exercises: list[Exercise] = Field(default_factory=list)
    progression_note: str = ""


class CoverageGap(BaseModel):
    refs: list[str] = Field(default_factory=list)
    description: str
    severity: Literal["low", "medium", "high"] = "medium"


class Coverage(BaseModel):
    segments_total: int = 0
    segments_referenced: int = 0
    beginning_covered: bool = False
    middle_covered: bool = False
    end_covered: bool = False
    demonstrations_covered: bool = False
    qa_covered: bool = False
    gaps: list[CoverageGap] = Field(default_factory=list)
    chunk_count: int = 0
    notes: list[str] = Field(default_factory=list)


class ImageRef(BaseModel):
    id: str
    path: str  # relative to lecture dir
    caption: str = ""
    source_file: str = ""
    page: int | None = None
    timestamp: float | None = None  # only when the source supports it
    ocr_text: str = ""


class LectureNotes(BaseModel):
    version: int = 1
    generated_at: str = ""
    model: str = ""
    source_kind: str = ""
    insufficiency_note: str = Field(default="", description="Explains why notes are limited when material is insufficient")
    overview: Overview = Field(default_factory=Overview)
    concepts: list[Concept] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    papers: list[Paper] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    practice: Practice = Field(default_factory=Practice)
    coverage: Coverage = Field(default_factory=Coverage)
    images: list[ImageRef] = Field(default_factory=list)
    global_flags: list[Flag] = Field(default_factory=list)


# ---------- intermediate (pipeline) models ----------

class InventoryItem(BaseModel):
    title: str
    kind: Literal["concept", "demonstration", "qa", "troubleshooting", "correction", "admin", "brief_mention"]
    summary: str
    start_ref: str
    end_ref: str
    importance: Literal["major", "minor"] = "major"


class MentionedTerm(BaseModel):
    term: str
    ref: str


class MentionedPaper(BaseModel):
    wording: str
    ref: str
    context: str = ""


class MentionedResource(BaseModel):
    wording: str
    url_text: str = ""
    ref: str
    purpose: str = ""


class ChunkInventory(BaseModel):
    chunk_summary: str = Field(description="3-6 sentence summary of this chunk for context continuity")
    items: list[InventoryItem]
    terms: list[MentionedTerm] = Field(default_factory=list)
    papers: list[MentionedPaper] = Field(default_factory=list)
    resources: list[MentionedResource] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    next_topic_announcement: str = Field(default="", description="Verbatim-ish wording if the instructor says what comes next")
    instructor_speaker_label: str = Field(default="", description="Which speaker label appears to be the instructor")


class OutlineConcept(BaseModel):
    id: str
    number: int
    title: str
    scope: str = Field(description="One or two sentences on what this concept covers")
    start_ref: str
    end_ref: str
    extra_refs: list[str] = Field(default_factory=list, description="Other refs where this concept is revisited")
    grouped_mentions: list[str] = Field(default_factory=list, description="Brief mentions folded in")
    is_substantive: bool = True


class ConceptOutline(BaseModel):
    concepts: list[OutlineConcept]
    rationale: str = ""


class Wrapup(BaseModel):
    overview: Overview
    summary: Summary
    practice: Practice
    glossary: list[GlossaryEntry]
    insufficiency_note: str = ""
    global_flags: list[Flag] = Field(default_factory=list)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema with additionalProperties=false and all properties required,
    as structured-output endpoints expect. Defaults are stripped."""
    schema = model.model_json_schema()

    def fix(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            node.pop("default", None)
            node.pop("title", None) if "properties" not in node and "type" in node and node.get("type") != "object" else None
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)

    fix(schema)
    return schema


# ---------- cross-lecture study guide ----------

class GuideConceptRef(BaseModel):
    lecture_id: str
    concept_id: str
    title: str


class GuideTheme(BaseModel):
    theme: str
    summary: str = Field(description="How the concepts under this theme fit together across lectures, for a beginner")
    concepts: list[GuideConceptRef] = Field(default_factory=list)


class GuidePathStep(BaseModel):
    step: int
    title: str
    what_to_do: str
    lecture_id: str = ""
    concept_ids: list[str] = Field(default_factory=list)
    minutes: int = 20


class GuideQuestionRef(BaseModel):
    lecture_id: str
    concept_id: str
    index: int = Field(description="Index of the interview question within that concept")


class GuideQuestionGroup(BaseModel):
    theme: str
    questions: list[GuideQuestionRef] = Field(default_factory=list)


class StudyGuide(BaseModel):
    title: str
    story: str = Field(description="How these lectures build on each other, told plainly for a beginner")
    themes: list[GuideTheme] = Field(default_factory=list)
    foundations: list[Foundation] = Field(default_factory=list, description="Merged, deduplicated background across lectures")
    learning_path: list[GuidePathStep] = Field(default_factory=list, description="Ordered study sessions of ~20 minutes")
    interview_bank: list[GuideQuestionGroup] = Field(default_factory=list, description="Existing interview questions grouped by theme (references only)")
    overlaps: list[str] = Field(default_factory=list, description="Ideas repeated across lectures and how the treatments differ")
    gaps: list[str] = Field(default_factory=list, description="Things assumed but never taught across the set")
    glossary: list[GlossaryEntry] = Field(default_factory=list, description="Merged glossary; refs hold lecture ids")
