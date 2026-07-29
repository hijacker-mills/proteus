"""
Study modes for the Qubi (intelliq) profile — ported from the intelliq-web AI Tutor.

The web tutor exposes three pedagogical modes (simple / exam / teach-back), each a
distinct system contract. ACAG ports them the stateless, model-agnostic way: a mode
is just a small instruction block appended to the system prompt per request via
`agent.run(extra_system=...)`. No wire-format or tool-loop change — the block is
expressed purely through Qubi's existing tools (lecture_evidence, quiz_generate,
exam_preferences, recall_grade) and the renderer markers the qubi-chat FORMAT_GUIDE
already documents (<quiz>, <key_points>, <practice_question>, <exam_tip>, <citations>).

A block is injected ONLY when a caller explicitly sends a mode (X-Qubi-Mode header or
body `mode`). Callers that send no mode (messaging channels, legacy routes) are
unaffected, so this is additive and safe.
"""
from __future__ import annotations

SIMPLE = (
    "STUDY MODE: SIMPLE — Explain clearly and get them to the point fast.\n"
    "- Lead with 1–2 plain-language sentences, then a <key_points> block of the essentials.\n"
    "- Reach for a ```mermaid diagram or image_search only when a visual genuinely helps.\n"
    "- Close with ONE <practice_question> to check understanding.\n"
    "- Stay concise; don't over-explain."
)

EXAM = (
    "STUDY MODE: EXAM — Drill the student with exam-style questions.\n"
    "- If you don't already know what they're revising, call exam_preferences and/or lecture_evidence first to ground on their material.\n"
    "- Ask ONE question at a time as a <quiz> block. Ground questions in their notes: call quiz_generate on the relevant note/lecture and build the questions strictly from what it returns — never quiz outside their material.\n"
    "- After each answer, give a one-sentence verdict, then the next question.\n"
    "- When the set is done, give a short score summary and one <exam_tip>."
)

TEACHBACK = (
    "STUDY MODE: TEACH-BACK — The student explains; you evaluate (the Feynman technique).\n"
    "- Start by asking them to explain a specific concept in their own words, rendered as a <practice_question>.\n"
    "- When they answer, ground it against their notes with lecture_evidence, then reply with a <key_points> block where each point is prefixed 'Strength:' or 'Gap:'.\n"
    "- Cite the sources you used with a <citations> block.\n"
    "- End with a new <practice_question> targeting their biggest gap."
)

MODE_BLOCKS = {"simple": SIMPLE, "exam": EXAM, "teachback": TEACHBACK}


def block_for(mode: str | None) -> str:
    """Return the system-prompt block for an explicit mode, or "" for none/unknown."""
    return MODE_BLOCKS.get((mode or "").strip().lower(), "")
