"""WhatsApp template contracts.

pause_confirmation and media_failure copy is proposed-for-review; parameter
shapes are the submission contract.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateCall:
    name: str
    params: list[str]
    language: str = "en"


TEMPLATES = {
    "escalation": {
        "param_count": 3,
        "copy": (
            "Hi {{1}}, this is your assistant. {{2}} has shared something I "
            "think is worth your attention soon. They haven't asked me to "
            "share specifics -- when you're ready, please reach out to them "
            "directly. {{3}}"
        ),
    },
    "pause_confirmation": {
        "param_count": 2,
        "copy": (
            "Hi {{1}}, {{2}} has paused our conversations for now. I'll be "
            "quiet on both threads until either of you messages me again."
        ),
    },
    "media_failure": {
        "param_count": 2,
        "copy": "Hi {{1}}, I couldn't process your last {{2}} note -- could you try resending or describe it in text?",
    },
}


def render_template(call: TemplateCall) -> dict:
    spec = TEMPLATES[call.name]
    if len(call.params) != spec["param_count"]:
        raise ValueError(f"{call.name} expects {spec['param_count']} params")
    return {
        "name": call.name,
        "language": {"code": call.language},
        "components": [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": param} for param in call.params],
            }
        ],
    }
