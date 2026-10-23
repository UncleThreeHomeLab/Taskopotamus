import os
from pathlib import Path


DEFAULT_PROFILE = {
    "identity": "You are Taskopotamus, a helpful personal assistant in Telegram.",
    "persona": "Be warm, direct, and concise. Match the user's language and provide detail when requested.",
    "behavior": "Use conversation history for follow-ups. Clarify important ambiguity and state uncertainty honestly.",
    "boundaries": "Respect my privacy. Ask for clarification when a requested persistent preference change is ambiguous.",
}


def load_system_instructions(path=None):
    path = Path(path or os.environ.get("AGENT_INSTRUCTIONS_FILE") or (
        Path(__file__).resolve().parents[2] / "config/agent/system_instructions.md"
    ))
    try:
        with path.open("rb") as source:
            content = source.read(16385)
        if len(content) > 16384:
            raise ValueError()
        text = content.decode("utf-8").strip()
        if not text:
            raise ValueError()
        return text
    except (OSError, UnicodeError, ValueError):
        raise ValueError("System instructions must be readable nonempty UTF-8 and at most 16384 bytes.") from None


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or not changes.keys() <= DEFAULT_PROFILE.keys():
        raise ValueError("Only identity, persona, behavior, and boundaries can be updated.")
    for value in changes.values():
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError("Each profile field must contain between 1 and 2000 characters.")
        if any(ord(character) < 32 and character not in "\n\t" for character in value):
            raise ValueError("Profile fields cannot contain control characters.")
    return {key: value.strip() for key, value in changes.items()}


def resolve_profile(profile):
    # Older saved profiles inherit defaults for fields added after they were created.
    return validate_changes({key: profile.get(key, value) for key, value in DEFAULT_PROFILE.items()})
