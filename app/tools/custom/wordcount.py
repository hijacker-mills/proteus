"""Example Python tool. Exports SCHEMA + handler; auto-discovered on load."""
SCHEMA = {
    "type": "function",
    "function": {
        "name": "wordcount",
        "description": "Count words and characters in a piece of text.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to measure"}},
            "required": ["text"],
        },
    },
}

async def handler(user_id: str, args: dict) -> dict:
    text = str(args.get("text", ""))
    return {"words": len(text.split()), "chars": len(text)}
