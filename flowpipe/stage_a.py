"""Stage A — turn a topic + master prompt into a validated manifest.

Two paths, both first-class:

* ``generate_via_api``   — headless Anthropic Messages API. Default for anything
  that must run unattended.
* ``write_manual_request`` — writes the assembled prompt to disk for a hand-run
  in Claude Desktop, then ``import_manifest`` validates whatever comes back.

The API path never trusts Claude's formatting: the reply is parsed strictly, and
on a schema failure the error text is fed back for one repair attempt before the
job is marked needs-review.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import projects
from .config import SETTINGS
from .models import Manifest
from .prompts import CLAUDE_DESKTOP_INSTRUCTIONS, render_prompt

logger = logging.getLogger(__name__)


class StageAError(RuntimeError):
    pass


def _extract_json(text: str) -> str:
    """Claude usually obeys "JSON only", but strip fences / prose defensively."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    if text.startswith("{"):
        return text
    start = text.find("{")
    if start == -1:
        raise StageAError("no JSON object found in Claude's reply")
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise StageAError("unterminated JSON object in Claude's reply")


#: printed whenever a paste cannot be parsed, so the shape is never a guessing game
MANIFEST_SHAPE = (
    '{"project": {"topic": "..."},\n'
    ' "characters": [{"name": "...", "face_prompt": "...", "body_prompt": "..."}],\n'
    ' "scenes": [{"id": "scene-01", "title": "...", "image_prompt": "...",\n'
    '             "animation_prompt": "...", "narration": "..."}]}'
)


def _unwrap(payload: Any) -> dict[str, Any]:
    """Accept the shapes a model actually emits, instead of only the ideal one.

    Seen in the wild: the whole manifest wrapped in ``{"manifest": {...}}``, and
    ``scenes`` nested inside ``project`` because the model treated the manifest as
    project metadata. Both are unambiguous, so lift them rather than failing with
    a pydantic message the operator cannot act on.
    """
    if not isinstance(payload, dict):
        raise StageAError(f"a manifest must be a JSON object, not {type(payload).__name__}")
    for key in ("manifest", "result", "output"):
        inner = payload.get(key)
        if isinstance(inner, dict) and ("scenes" in inner or "project" in inner):
            logger.info("manifest was wrapped in %r; unwrapped it", key)
            payload = inner
            break
    if "scenes" not in payload:
        nested = payload.get("project")
        if isinstance(nested, dict) and "scenes" in nested:
            logger.info("scenes were nested under project; lifted them")
            meta = {k: v for k, v in nested.items() if k != "scenes"}
            payload = {**payload, "project": meta, "scenes": nested["scenes"]}
    if "project" not in payload:
        payload = {"project": {"topic": payload.get("topic", "")}, **payload}
    return payload


def _explain(exc: Exception) -> str:
    """Turn a pydantic failure into lines an operator can act on."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    lines = []
    for error in errors()[:6]:
        where = ".".join(str(part) for part in error.get("loc", ())) or "manifest"
        lines.append(f"  {where}: {error.get('msg')}")
    return "\n".join(lines) or str(exc)


def _validate_payload(payload: Any) -> Manifest:
    try:
        return Manifest.model_validate(_unwrap(payload))
    except StageAError:
        raise
    except Exception as exc:
        raise StageAError(
            f"this is not a valid manifest:\n{_explain(exc)}\n\n"
            f"A manifest must have a `scenes` array at the top level:\n{MANIFEST_SHAPE}"
        ) from exc


def _validate(raw: str) -> Manifest:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StageAError(
            f"that is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})."
        ) from exc
    return _validate_payload(payload)


SYSTEM_PROMPT = (
    "You are a production-planning tool. You reply with a single JSON "
    "object and nothing else."
)


def _deepseek_chat(
    messages: list[dict[str, str]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 180.0,
) -> str:
    """One OpenAI-compatible chat completion, using only the standard library.

    DeepSeek's API is a drop-in for the OpenAI chat shape, so adding it costs no
    dependency (the repository deliberately does not vendor an SDK it would then
    have to track). Any OpenAI-compatible base URL works.
    """
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions" if url.endswith("/v1") else "/v1/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
        "temperature": 0.7,
        "max_tokens": 8000,
        "stream": False,
    }).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        hint = ""
        if exc.code == 401:
            hint = " - check the API key"
        elif exc.code == 402:
            hint = " - the DeepSeek account has no balance"
        elif exc.code == 404:
            hint = f" - is {base_url} the right base URL? (no /chat/completions suffix needed)"
        raise StageAError(f"DeepSeek API returned HTTP {exc.code}{hint}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise StageAError(f"could not reach the DeepSeek API at {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise StageAError(f"DeepSeek returned something that is not JSON: {exc}") from exc
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise StageAError(f"unexpected DeepSeek response: {json.dumps(payload)[:400]}") from exc


def generate_via_api(
    *,
    prompt: str,
    model: str | None = None,
    api_key: str | None = None,
    client: Any = None,
    provider: str | None = None,
    chat: Any = None,
) -> tuple[Manifest, str]:
    """Return (manifest, raw_reply). Raises StageAError on any failure.

    ``provider`` defaults to ``stage_a_provider`` in Settings: either ``anthropic``
    (Messages API through the SDK) or ``deepseek`` (any OpenAI-compatible endpoint,
    standard library only). Both share one repair loop, so a badly shaped reply is
    retried once with the validation error fed back whichever model produced it.
    """
    provider = (provider or SETTINGS.stage_a_provider or "anthropic").strip().lower()
    model_name = model or (
        SETTINGS.deepseek_model if provider == "deepseek" else SETTINGS.anthropic_model
    )

    if chat is not None:                      # injected for tests / other clients
        call = chat
    elif provider == "deepseek":
        key = api_key or SETTINGS.deepseek_api_key
        base_url = SETTINGS.deepseek_base_url or "https://api.deepseek.com"
        if not key:
            raise StageAError(
                "DEEPSEEK_API_KEY is not set. Put it in the environment, set it in "
                "Settings, or use the manual path for this run."
            )

        def call(messages: list[dict[str, str]]) -> str:
            return _deepseek_chat(messages, base_url=base_url, model=model_name, api_key=key)
    else:
        key = api_key or SETTINGS.anthropic_api_key
        if client is None:
            if not key:
                raise StageAError(
                    "ANTHROPIC_API_KEY is not set. Add it to the environment, or use the "
                    "manual Claude Desktop path for this run."
                )
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise StageAError("the `anthropic` package is not installed") from exc
            client = anthropic.Anthropic(api_key=key)

        def call(messages: list[dict[str, str]]) -> str:
            try:
                response = client.messages.create(
                    model=model_name,
                    max_tokens=8000,
                    temperature=0.7,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                )
            except Exception as exc:
                raise StageAError(f"Anthropic API call failed: {exc}") from exc
            return "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )

    messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        text = call(messages)
        try:
            manifest = _validate(_extract_json(text))
        except Exception as exc:
            if attempt == 1:
                raise StageAError(
                    f"{provider}'s manifest failed validation twice: {exc}"
                ) from exc
            messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "That reply could not be parsed as a valid manifest. Error:\n"
                        f"{exc}\n\nReturn ONLY the corrected JSON object."
                    ),
                },
            ]
            continue
        manifest.generator = provider
        return manifest, text
    raise StageAError("unreachable")


def write_manual_request(project_id: str, prompt: str) -> Path:
    path = projects.requests_dir(project_id) / "stage-a-prompt.md"
    path.write_text(
        CLAUDE_DESKTOP_INSTRUCTIONS.format(
            manifest_out=str(projects.manifest_path(project_id)), prompt=prompt
        ),
        encoding="utf-8",
    )
    return path


def import_manifest(project_id: str, raw: str | dict) -> Manifest:
    """Validate a hand-pasted manifest and persist it."""
    manifest = _validate_payload(raw) if isinstance(raw, dict) else _validate(_extract_json(str(raw)))
    projects.write_manifest(project_id, manifest)
    return manifest


def build_prompt(
    *,
    topic: str,
    template_id: str,
    scene_count: int,
    target_duration: int,
    style: str = "cinematic, photoreal, shallow depth of field",
    tone: str = "warm and curious",
    aspect_ratio: str = "16:9",
    extra: str = "",
) -> str:
    return render_prompt(
        template_id,
        topic=topic,
        scene_count=scene_count,
        target_duration=target_duration,
        style=style,
        tone=tone,
        aspect_ratio=aspect_ratio,
        extra=extra,
    )
