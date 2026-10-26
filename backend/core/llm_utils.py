"""LLM response helpers shared by Anthropic-compatible providers."""
import json
import os
from urllib.parse import urlparse
from typing import Any, Iterable, List


class LLMOutputError(ValueError):
    """A visible output problem, with a stable reason for diagnostics."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def short_task_options(client: Any, model: str) -> dict:
    """Disable thinking for short tasks only on verified provider/model pairs.

    auto is deliberately provider/model scoped. Other providers retain their
    defaults; an explicit disabled/default setting can override auto.
    """
    mode = os.getenv("ORDO_SHORT_TASK_THINKING", "auto").lower().strip()
    host = urlparse(str(getattr(client, "base_url", ""))).hostname
    model_name = model.lower().strip()
    verified = (
        host == "api-inference.modelscope.cn" and "deepseek" in model_name
    ) or (
        # Confirmed with the same production rewrite prompt and token budget.
        host == "api.deepseek.com" and model_name == "deepseek-flash"
    )
    if mode == "disabled" or (mode == "auto" and verified):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def response_metadata(response: Any) -> dict:
    """Never include hidden thinking or complete prompts in diagnostics."""
    usage = getattr(response, "usage", None)
    return {
        "stop_reason": getattr(response, "stop_reason", None),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "text_chars": len(extract_text_content(getattr(response, "content", []))),
    }


def completed_text(response: Any) -> str:
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise LLMOutputError("output_truncated")
    text = extract_text_content(getattr(response, "content", [])).strip()
    if not text:
        raise LLMOutputError("empty_text")
    return text


def parse_json_response(response: Any, expected_type: type) -> Any:
    text = completed_text(response)
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if lines[0].strip().lower() in ("```", "```json"):
            text = "\n".join(lines[1:-1]).strip()
    def reject_constant(value):
        raise ValueError(value)
    try:
        data = json.loads(text, parse_constant=reject_constant)
    except (ValueError, TypeError) as exc:
        raise LLMOutputError("invalid_json") from exc
    if not isinstance(data, expected_type):
        raise LLMOutputError("invalid_json_type")
    return data


def extract_text_content(content: Iterable[Any], separator: str = "\n") -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return separator.join(t for t in texts if t)
