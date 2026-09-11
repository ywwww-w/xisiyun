from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas import LLMSummaryResponse
from app.utils.errors import (
    BadRequestException,
    LLMCallException,
    LLMResponseFormatException,
)
from app.utils.logger import get_logger

_logger = get_logger("app.services.llm")

SYSTEM_PROMPT: str = (
    "You are a precise transcript summarizer. "
    "You MUST output a valid JSON object with EXACTLY 3 keys: "
    "'summary' (string, one-sentence high-level overview), "
    "'key_points' (array of strings, 2~5 bullet points), "
    "'todos' (array of strings, 0~3 action items extracted). "
    "If no todos exist return empty array []. "
    "Do NOT include any markdown fences, explanations, or extra keys outside the JSON object."
)

USER_PROMPT_TEMPLATE: str = (
    "Here is the transcript:\n\n{transcript}\n\n"
    "Now output the summary JSON per instructions:"
)

_client: httpx.AsyncClient | None = None
_settings_snapshot: Settings | None = None
_MAX_TRANSCRIPT_CHARS: int = 20000  # safety truncate (approx < 20k chars ~ 15k tokens)


def init_client(settings: Settings) -> httpx.AsyncClient:
    """Initialize module-level httpx.AsyncClient with deepseek config.

    - timeout = (deepseek_timeout_seconds total, connect=10s)
    - base_url = settings.deepseek_base_url
    - Authorization Bearer <key>
    - http2=True
    If DEEPSEEK_API_KEY is empty (not configured yet), LOG ERROR but DO NOT
    crash app — later calls will raise LLMCallException with a clear message.
    Idempotent: re-calling recreates client with fresh settings.
    """
    global _client, _settings_snapshot
    if _client is not None:
        try:
            import asyncio

            asyncio.get_running_loop().create_task(_client.aclose())
        except Exception:  # pragma: no cover - defensive
            pass
        _client = None

    if not settings.deepseek_api_key:
        _logger.error(
            "[llm] init_client called with empty DEEPSEEK_API_KEY. "
            "Summarize calls will fail with LLM_CALL_ERROR until key is configured."
        )
    timeout = httpx.Timeout(float(settings.deepseek_timeout_seconds), connect=10.0)
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key or ''}",
        "Accept": "application/json",
    }
    http2_enabled = True
    try:
        _client = httpx.AsyncClient(
            base_url=settings.deepseek_base_url,
            timeout=timeout,
            headers=headers,
            http2=http2_enabled,
        )
    except ImportError as exc:
        # Fallback: http2 bonus needs h2; graceful degrade to http1.1 without crashing.
        if "http2=True" in str(exc) or "h2" in str(exc).lower():
            http2_enabled = False
            _logger.warning(
                "[llm] 'h2' package not installed (got %s); falling back to http1.1. "
                "Run `pip install httpx[http2]` for bonus http2 performance.",
                exc,
            )
            _client = httpx.AsyncClient(
                base_url=settings.deepseek_base_url,
                timeout=timeout,
                headers=headers,
                http2=False,
            )
        else:
            raise
    _settings_snapshot = settings
    _logger.info(
        "[llm] httpx client initialized base_url=%s model=%s timeout=%s http2=%s",
        settings.deepseek_base_url,
        settings.deepseek_model,
        settings.deepseek_timeout_seconds,
        http2_enabled,
    )
    return _client


async def shutdown_client() -> None:
    """Close httpx client if initialized; always safe to call twice."""
    global _client, _settings_snapshot
    if _client is None:
        _logger.info("[llm] shutdown_client called: no client running, skip")
        _settings_snapshot = None
        return
    try:
        await _client.aclose()
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning("[llm] shutdown_client aclose raised (swallow): %s", exc)
    finally:
        _client = None
        _settings_snapshot = None
    _logger.info("[llm] httpx client shutdown complete")


async def summarize_transcript(transcript: str) -> dict[str, Any]:
    """Call DeepSeek chat completion and return {summary, key_points, todos}.

    Three safety layers per spec R7:
        1. request JSON with response_format={"type":"json_object"}
        2. json.loads(content) — catches plain text / markdown fenced JSON
        3. LLMSummaryResponse.model_validate() — key_points min_length=1 etc.

    Empty transcript / len < 10: raise BadRequest (no network call, T8 验收5).
    No client initialized: init client lazily from Settings() (convenience).
    """
    global _client, _settings_snapshot
    if not isinstance(transcript, str):
        raise BadRequestException("Transcript must be a string")
    if len(transcript.strip()) < 10:
        raise BadRequestException("Transcript too short (< 10 chars) to summarize")

    if _client is None:
        init_client(Settings())
    assert _client is not None and _settings_snapshot is not None

    model = _settings_snapshot.deepseek_model
    timeout_s = _settings_snapshot.deepseek_timeout_seconds
    truncated = transcript[:_MAX_TRANSCRIPT_CHARS]
    _logger.info(
        "[llm] summarize input_chars=%d truncated_chars=%d model=%s timeout_s=%s",
        len(transcript),
        len(truncated),
        model,
        timeout_s,
    )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(transcript=truncated)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "max_tokens": 2048,
        "stream": False,
    }

    # --- LAYER 0: raw HTTP call / upstream status ---
    upstream_status: int | None = None
    try:
        resp = await _client.post("/chat/completions", json=payload)
        upstream_status = resp.status_code
        if upstream_status != 200:
            # include first 500 chars of body in details (helpful for 401 key / 429)
            raise LLMCallException(
                message=(
                    f"LLM upstream returned HTTP {upstream_status} "
                    f"(expected 200). model={model!r}"
                ),
                upstream_status=upstream_status,
                details={
                    "response_body_preview": resp.text[:500],
                    "requested_model": model,
                },
            )
        try:
            resp_json = resp.json()
        except ValueError as exc:  # httpx raises ValueError for non-JSON bodies
            raise LLMResponseFormatException(
                message="LLM response body is not valid JSON at top-level (httpx resp.json())",
                raw_preview=resp.text[:300],
            ) from exc
    except httpx.TimeoutException as exc:
        raise LLMCallException(
            message=f"LLM call timed out after {timeout_s}s (connect/overall).",
            upstream_status=upstream_status,
            details={"timeout_seconds": timeout_s},
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMCallException(
            message=f"LLM transport error: {type(exc).__name__}: {exc}",
            upstream_status=upstream_status,
        ) from exc

    # --- Extract content from choices[0].message.content ---
    try:
        content = resp_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMCallException(
            message="Unexpected response schema from LLM (no choices/message/content).",
            upstream_status=upstream_status,
            details={"raw_top_keys": sorted(resp_json.keys()) if isinstance(resp_json, dict) else None},
        ) from exc
    if not isinstance(content, str):
        raise LLMResponseFormatException(
            message="LLM message.content is not a string",
            raw_preview=str(content)[:200],
        )
    content_stripped = content.strip()
    if not content_stripped:
        raise LLMResponseFormatException(
            message="LLM message.content is empty",
            raw_preview="",
        )

    # --- LAYER 2: json.loads ---
    try:
        parsed: Any = json.loads(content_stripped)
    except json.JSONDecodeError as exc:
        # Markdown fenced JSON fallback — strip ```json ... ``` fences if present
        fallback = _try_strip_markdown_json_fence(content_stripped)
        if fallback is None:
            raise LLMResponseFormatException(
                message=f"Invalid JSON from LLM content: {exc.msg} (line {exc.lineno} col {exc.colno})",
                raw_preview=content_stripped[:400],
            ) from exc
        try:
            parsed = json.loads(fallback)
        except json.JSONDecodeError as exc2:
            raise LLMResponseFormatException(
                message=f"Invalid JSON from LLM (even after fence strip): {exc2.msg}",
                raw_preview=content_stripped[:400],
            ) from exc2

    if not isinstance(parsed, dict):
        raise LLMResponseFormatException(
            message="LLM JSON root must be an object ({...})",
            raw_preview=str(parsed)[:200],
        )

    # --- LAYER 3: Pydantic schema (LLMSummaryResponse key_points min_length=1) ---
    try:
        validated = LLMSummaryResponse.model_validate(parsed)
    except ValidationError as exc:
        raise LLMResponseFormatException(
            message=(
                "LLM JSON missing required keys / wrong types. "
                f"Details: {exc.errors(include_url=False, include_context=False)}"
            ),
            raw_preview=json.dumps(parsed, ensure_ascii=False)[:400],
        ) from exc

    out: dict[str, Any] = validated.model_dump()
    _logger.info(
        "[llm] summarize SUCCESS output_keys=%s summary_len=%d key_points=%d todos=%d",
        sorted(out.keys()),
        len(out["summary"]),
        len(out["key_points"]),
        len(out["todos"]),
    )
    # post-condition: exactly 3 keys (summary/key_points/todos)
    assert set(out.keys()) == {"summary", "key_points", "todos"}, set(out.keys())
    return out


def _try_strip_markdown_json_fence(content: str) -> str | None:
    """If content is wrapped in ```json ... ``` fences, return inner JSON text; else None."""
    s = content.strip()
    if not s.startswith("```"):
        return None
    # drop opening ``` and optional json tag
    first_line_end = s.find("\n")
    if first_line_end == -1:
        return None
    body = s[first_line_end + 1 :]
    # closing ```
    closing = body.rfind("```")
    if closing == -1:
        return None
    return body[:closing].strip()
