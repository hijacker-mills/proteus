"""
Tool schemas in OpenAI function-calling format — the provider-neutral lingua
franca. LiteLLM translates these to each backend's native format (Anthropic
tool_use, OpenAI tools, Gemini function declarations, etc.), so the same
definitions work regardless of which model is configured.

NOTE: `user_id` is deliberately ABSENT from every schema. acag injects the
authenticated user_id server-side at dispatch time. The model cannot see or
spoof it — per-user isolation is enforced by the gateway, not model compliance.
"""
from __future__ import annotations


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOLS = [
    _fn(
        "lecture_evidence",
        "Retrieve evidence chunks from the student's lecture notes for a query. "
        "Always call this before answering a content question.",
        {
            "query": {"type": "string", "description": "What to search the notes for"},
            "scope": {"type": "string", "enum": ["lecture", "course", "library"], "default": "library"},
            "lecture_id": {"type": "string", "description": "Scope to a specific lecture"},
            "course_id": {"type": "string", "description": "Scope to a specific course"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        ["query"],
    ),
    _fn(
        "exam_preferences",
        "Get the student's upcoming exams and study preferences.",
        {"action": {"type": "string", "enum": ["get"], "default": "get"}},
        [],
    ),
    _fn(
        "quiz_generate",
        "Generate quiz questions from a note or lecture. "
        "Call this instead of improvising questions yourself.",
        {
            "note_id": {"type": "string", "description": "Target note ID (optional if lecture_id given)"},
            "lecture_id": {"type": "string", "description": "Target lecture ID (optional if note_id given)"},
            "count": {"type": "integer", "minimum": 3, "maximum": 20, "default": 5},
            "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"], "default": "medium"},
        },
        [],
    ),
    _fn(
        "recall_grade",
        "Grade a student's free-recall attempt against their note. "
        "Call this instead of grading with a heuristic.",
        {
            "note_id": {"type": "string"},
            "attempt": {"type": "string", "description": "The student's written recall attempt"},
        },
        ["note_id", "attempt"],
    ),
    _fn(
        "image_search",
        "Search for educational images (diagrams, anatomy, charts, maps) relevant to a "
        "study topic. Prefer this over describing an image in words.",
        {
            "query": {"type": "string", "description": "Descriptive search query"},
            "count": {"type": "integer", "minimum": 2, "maximum": 6, "default": 4},
        },
        ["query"],
    ),
    _fn(
        "memory_read",
        "Retrieve recent stored memories about the student (chronological). "
        "Use at session start to load context.",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            "memory_type": {"type": "string", "enum": ["user_memory", "mastery_signal"], "default": "user_memory"},
        },
        [],
    ),
    _fn(
        "memory_write",
        "Persist a learned fact or preference about the student for future sessions.",
        {
            "text": {"type": "string", "description": "The memory content to store"},
            "memory_type": {"type": "string", "enum": ["user_memory", "mastery_signal"], "default": "user_memory"},
        },
        ["text"],
    ),
    _fn(
        "memory_search",
        "Semantic search over the student's stored memories. "
        "Use to surface relevant past context before answering.",
        {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            "memory_type": {"type": "string", "enum": ["user_memory", "mastery_signal"]},
        },
        ["query"],
    ),
]
