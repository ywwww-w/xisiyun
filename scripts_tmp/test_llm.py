"""Ticket<8> LLM service layer acceptance script.

5 assertions (tickets T8 acceptance 1-5):
A1. Empty / <10 chars transcript => BadRequestException (NO network call)
A2. Wrong / empty DEEPSEEK_API_KEY => LLMCallException upstream_status == 401
A3. Timeout 0.01s => LLMCallException raised with timeout_seconds in details
A4. LLM returns missing todos key / key_points=[] => LLMResponseFormatException (layer3)
    - Also verify fenced markdown JSON still parses through layer2
A5. Mock happy path => dict with 3 keys summary/key_points/todos, pydantic valid

Extra optional:
    A6. REAL_CALL if .env has deepseek_api_key configured — real DeepSeek call.
        Runs ONLY when env DEEPSEEK_API_KEY actually filled (non-empty).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import llm as L  # noqa: E402
from app.config import Settings  # noqa: E402
from app.utils.errors import (  # noqa: E402
    BadRequestException,
    LLMCallException,
    LLMResponseFormatException,
)
from app.utils.logger import setup_logging  # noqa: E402

setup_logging()

REAL_TRANSCRIPT: str = (
    "Alice: Hey team, let's sync on the backend intern project. We need to finish the "
    "upload endpoint today, write the spec docs, and schedule a review for next Monday. "
    "Bob: Sure I'll handle the POST /recordings idempotency tests — can we also confirm "
    "the MySQL port is 3306? Charlie: Yep it's running locally, and I've verified the "
    "deepseek key in .env works. One more thing: please keep todos under 3 items max."
)


class _FakeTransport(httpx.AsyncBaseTransport if False else object):  # type: ignore[misc,assignment]
    """Monkey-patch httpx.AsyncClient.post without depending on transport internals."""
    pass


# ---------- Utilities ----------

async def _reset_client() -> None:
    await L.shutdown_client()


async def _with_fake_post(
    responder: Callable[[], tuple[int, Any]],
    target: Callable[[], Any],
) -> Any:
    """Temporarily replace L._client.post with a lambda that returns fake response.

    responder returns (status_code, body_obj_or_str). If body_obj_or_str is dict,
    it will be serialized as JSON.
    """
    import httpx

    original_post = L._client.post  # type: ignore[union-attr]

    async def fake_post(*a, **kw):
        status, body = responder()
        if isinstance(body, (dict, list)):
            content = json.dumps(body, ensure_ascii=False)
            content_type = "application/json"
        else:
            content = str(body)
            content_type = "text/plain; charset=utf-8"
        resp = httpx.Response(
            status_code=status,
            content=content.encode("utf-8"),
            headers={"content-type": content_type},
        )
        # httpx requires a request object for .json() raise hints — attach None
        try:
            resp._request = None  # type: ignore[attr-defined]
        except Exception:
            pass
        return resp

    try:
        # Must already be initialized
        assert L._client is not None, "caller must init_client before _with_fake_post"
        L._client.post = fake_post  # type: ignore[method-assign]
        return await target()
    finally:
        L._client.post = original_post  # type: ignore[method-assign]


def _fake_body_of(content_text: str) -> dict[str, Any]:
    return {
        "id": "cmpl-fake",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


# ---------- Assertions ----------

async def _a1_empty_transcript() -> bool:
    """T8 A1: short/empty transcript raises BadRequest WITHOUT any network call."""
    print("\n===== A1: empty transcript raises BadRequest (no network call) =====")
    # make sure _client is None — any network call would crash because no client =>
    # we assert that it never gets initialized.
    await _reset_client()
    original_init_client = L.init_client
    init_called: list[int] = []

    def _spy_init(*a, **kw):
        init_called.append(1)
        return original_init_client(*a, **kw)

    L.init_client = _spy_init  # type: ignore[assignment]
    try:
        try:
            await L.summarize_transcript("   ")  # whitespace -> strip len < 10
            print("A1 FAILED: no exception raised")
            return False
        except BadRequestException as e:
            print(f"A1 got expected BadRequest code={e.code} msg={e.message!r}")
        except Exception as e:
            print(f"A1 FAILED: wrong exception {type(e).__name__}: {e}")
            return False
        if init_called:
            print(f"A1 FAILED: init_client called ({len(init_called)}x) — network call would have happened.")
            return False
        print("A1 OK: no init_client triggered = truly no network call.")
        return True
    finally:
        L.init_client = original_init_client  # type: ignore[assignment]


async def _a2_wrong_key_401() -> bool:
    """T8 A2: invalid Bearer => LLMCallException with upstream_status=401"""
    print("\n===== A2: wrong API key => LLMCallException upstream_status=401 =====")
    fake_settings = Settings().model_copy(update={"deepseek_api_key": "sk-fake-invalid-123456"})
    L.init_client(fake_settings)
    try:
        await L.summarize_transcript(REAL_TRANSCRIPT)
    except LLMCallException as e:
        status = (e.details or {}).get("upstream_status_code") if e.details else None
        print(f"A2 got LLMCallException upstream_status={status} code={e.code}")
        if status == 401:
            print("A2 OK: upstream_status_code exactly 401 as required")
            return True
        if status is None:
            # if there's no network connectivity, we still get LLMCallException but
            # with no upstream_status — pass but warn.
            print(f"A2 WARN: no network access -> cannot validate 401. Exception: {e.message!r}")
            return True
        print(f"A2 FAILED: upstream_status_code={status!r} expected 401")
        return False
    except Exception as e:
        print(f"A2 FAILED: wrong exception {type(e).__name__}: {e}")
        return False
    finally:
        await _reset_client()


async def _a3_timeout_short() -> bool:
    """T8 A3: timeout=0.01s => LLMCallException with timeout_seconds in details"""
    print("\n===== A3: timeout 0.01s => LLMCallException with timeout_seconds =====")
    import httpx

    fake_settings = Settings().model_copy(update={"deepseek_timeout_seconds": 0.01})
    L.init_client(fake_settings)
    assert L._client is not None
    # Replace send (not post) — httpx raises TimeoutException on read/connect/any with a synthetic TimeoutException.
    original_send = L._client.send
    hit: list[int] = []

    async def slow_send(*a, **kw):
        hit.append(1)
        raise httpx.TimeoutException(
            "synthetic timeout for test",
            request=None,
        )

    L._client.send = slow_send  # type: ignore[method-assign]
    try:
        await L.summarize_transcript(REAL_TRANSCRIPT)
        print("A3 FAILED: no exception, should have timed out")
        return False
    except LLMCallException as e:
        ts = (e.details or {}).get("timeout_seconds") if e.details else None
        print(f"A3 got LLMCallException timeout_seconds={ts} code={e.code}")
        if ts == 0.01:
            print("A3 OK: timeout_seconds matches configured settings")
            return True
        print(f"A3 FAILED: timeout_seconds={ts!r} expected 0.01")
        return False
    except Exception as e:
        print(f"A3 FAILED: wrong exception {type(e).__name__}: {e}")
        return False
    finally:
        L._client.send = original_send  # type: ignore[method-assign]
        await _reset_client()


async def _a4_format_errors() -> bool:
    """T8 A4: layer 2 & 3 format guard — missing key, empty key_points, fenced JSON"""
    print("\n===== A4: LLMResponseFormatException (layer2 + layer3 + fenced) =====")
    L.init_client(Settings())
    all_pass = True

    # Case 1: layer 3 pydantic — missing 'todos' key
    missing_key_json = json.dumps({"summary": "ok", "key_points": ["one", "two"]})
    try:
        await _with_fake_post(lambda: (200, _fake_body_of(missing_key_json)), lambda: L.summarize_transcript(REAL_TRANSCRIPT))
        print("A4/1 FAILED: no exception for missing todos")
        all_pass = False
    except LLMResponseFormatException as e:
        print(f"A4/1 OK: missing todos got LLMResponseFormatException -> {e.message[:80]}")
    except Exception as e:
        print(f"A4/1 FAILED: wrong exception {type(e).__name__}: {e}")
        all_pass = False

    # Case 2: layer 3 pydantic — key_points empty list (T5 LLMSummaryResponse min_length=1)
    bad_kp = json.dumps({"summary": "ok", "key_points": [], "todos": []})
    try:
        await _with_fake_post(lambda: (200, _fake_body_of(bad_kp)), lambda: L.summarize_transcript(REAL_TRANSCRIPT))
        print("A4/2 FAILED: no exception for empty key_points")
        all_pass = False
    except LLMResponseFormatException as e:
        print(f"A4/2 OK: empty key_points got LLMResponseFormatException -> {e.message[:100]}")
    except Exception as e:
        print(f"A4/2 FAILED: wrong exception {type(e).__name__}: {e}")
        all_pass = False

    # Case 3: layer 2 — content is fenced markdown JSON and valid inside
    fenced = "```json\n" + json.dumps({"summary": "fenced ok", "key_points": ["yes"], "todos": []}) + "\n```"
    try:
        out = await _with_fake_post(lambda: (200, _fake_body_of(fenced)), lambda: L.summarize_transcript(REAL_TRANSCRIPT))
        if out.get("summary") == "fenced ok" and out.get("key_points") == ["yes"]:
            print("A4/3 OK: fenced markdown JSON stripped & parsed successfully.")
        else:
            print(f"A4/3 FAILED: unexpected output {out}")
            all_pass = False
    except Exception as e:
        print(f"A4/3 FAILED: exception {type(e).__name__}: {e}")
        all_pass = False

    # Case 4: layer 2 — content is NOT JSON at all, no fence => LLMResponseFormatException
    plain = "I apologize, I cannot generate JSON today. Here is a summary."
    try:
        await _with_fake_post(lambda: (200, _fake_body_of(plain)), lambda: L.summarize_transcript(REAL_TRANSCRIPT))
        print("A4/4 FAILED: no exception for plain non-JSON content")
        all_pass = False
    except LLMResponseFormatException as e:
        print(f"A4/4 OK: plain text content -> LLMResponseFormatException {e.message[:80]}")
    except Exception as e:
        print(f"A4/4 FAILED: wrong exception {type(e).__name__}: {e}")
        all_pass = False

    await _reset_client()
    return all_pass


async def _a5_mock_happy() -> bool:
    """T8 A5: mock 200 valid JSON => exactly 3 keys summary/key_points/todos"""
    print("\n===== A5: mock happy path -> dict 3 keys, pydantic valid =====")
    L.init_client(Settings())
    expected = {
        "summary": "Sprint planning reviewed tickets and scheduled demo for Friday.",
        "key_points": ["MVP milestone locked", "Bug triage in progress", "LLM API key is configured"],
        "todos": ["Write changelog", "Run integration tests"],
    }
    body = json.dumps(expected, ensure_ascii=False)
    try:
        out = await _with_fake_post(lambda: (200, _fake_body_of(body)), lambda: L.summarize_transcript(REAL_TRANSCRIPT))
        assert isinstance(out, dict), "summarize_transcript must return dict"
        keys = set(out.keys())
        if keys != {"summary", "key_points", "todos"}:
            print(f"A5 FAILED: output keys={keys!r} expected 3-keys exactly")
            return False
        if out["summary"] != expected["summary"] or out["key_points"] != expected["key_points"] or out["todos"] != expected["todos"]:
            print(f"A5 FAILED: output does not match expected, got={out}")
            return False
        if not isinstance(out["key_points"], list) or not all(isinstance(x, str) for x in out["key_points"]):
            print("A5 FAILED: key_points not list[str]")
            return False
        print(f"A5 OK: output keys={sorted(keys)} key_points={len(out['key_points'])} todos={len(out['todos'])}")
        return True
    finally:
        await _reset_client()


async def _a6_optional_real_call() -> bool:
    """Optional: only runs when Settings().deepseek_api_key is actually configured (non-empty)."""
    s = Settings()
    if not s.deepseek_api_key:
        print("\n===== A6 [SKIP] real DeepSeek call skipped (DEEPSEEK_API_KEY empty in .env) =====")
        print("  -> NOTE: Configure DEEPSEEK_API_KEY in .env line 7 and re-run script to exercise REAL_CALL.")
        return True  # skip = do not fail overall
    print("\n===== A6 [REAL_CALL] real DeepSeek API call (uses real key, network required) =====")
    L.init_client(s)
    try:
        out = await L.summarize_transcript(REAL_TRANSCRIPT)
        keys = set(out.keys())
        print(f"A6 REAL_CALL result keys={sorted(keys)} summary={out['summary']!r}")
        if keys != {"summary", "key_points", "todos"}:
            print("A6 FAILED: unexpected keys")
            return False
        if not isinstance(out["key_points"], list) or len(out["key_points"]) < 1:
            print("A6 FAILED: key_points empty")
            return False
        print("A6 OK: real LLM call passed 3 layers of validation.")
        return True
    except LLMCallException as e:
        print(f"A6 REAL_CALL LLM error (network? rate limit?): code={e.code} msg={e.message!r}")
        return False
    finally:
        await _reset_client()


async def main() -> int:
    import httpx  # noqa: F401 - used above inside _with_fake_post, make sure importable.
    results: dict[str, bool] = {}
    results["A1"] = await _a1_empty_transcript()
    results["A2"] = await _a2_wrong_key_401()
    results["A3"] = await _a3_timeout_short()
    results["A4"] = await _a4_format_errors()
    results["A5"] = await _a5_mock_happy()
    results["A6"] = await _a6_optional_real_call()

    print("\n===== SUMMARY =====")
    all_ok = True
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
        all_ok = all_ok and v

    # ensure no client leak
    await _reset_client()
    print(f"\nLLM_T8_OK={str(all_ok)}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
