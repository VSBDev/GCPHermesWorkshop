#!/usr/bin/env python3
"""OpenAI-compatible proxy for Vertex AI open models.

Hermes can already talk to OpenAI-compatible endpoints. Vertex AI's OpenAI
compatibility uses Google Cloud authentication rather than static API keys, so
this proxy refreshes Application Default Credentials and forwards requests to
Vertex AI on behalf of Hermes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
import json
from typing import Any, AsyncIterator

import google.auth
import google.auth.transport.requests
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


logger = logging.getLogger("vertex_openai_proxy")

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
MODEL_ID = os.getenv("MODEL_ID", "google/gemma-4-26b-a4b-it-maas")
LOCATION = os.getenv("LOCATION", "global")
TIMEOUT_SECONDS = float(os.getenv("VERTEX_TIMEOUT_SECONDS", "600"))
STREAM_MODE = os.getenv("VERTEX_STREAM_MODE", "synthetic").strip().lower() or "synthetic"
DEBUG_DUMP = os.getenv("VERTEX_DEBUG_DUMP", "").lower() in {"1", "true", "yes"}
ALLOW_MODEL_OVERRIDE = os.getenv("ALLOW_MODEL_OVERRIDE", "").lower() in {
    "1",
    "true",
    "yes",
}


def _vertex_host(location: str) -> str:
    if location == "global":
        return "https://aiplatform.googleapis.com"
    return f"https://{location}-aiplatform.googleapis.com"


def _canonical_model_id(model_id: str) -> str:
    model_id = str(model_id or "").strip()
    if not model_id:
        return "google/gemma-4-26b-a4b-it-maas"
    if "/" in model_id:
        return model_id
    return f"google/{model_id}"


class AccessTokenProvider:
    def __init__(self) -> None:
        configured_project = (
            os.getenv("PROJECT_ID")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GCLOUD_PROJECT")
            or ""
        ).strip()
        credentials, detected_project = google.auth.default(scopes=SCOPES)
        self.credentials = credentials
        self.project_id = configured_project or (detected_project or "").strip()
        if not self.project_id:
            raise RuntimeError(
                "PROJECT_ID is not set and could not be discovered from ADC."
            )
        self._request = google.auth.transport.requests.Request()
        self._lock = threading.Lock()

    def _needs_refresh(self) -> bool:
        if not self.credentials.valid:
            return True
        expiry = getattr(self.credentials, "expiry", None)
        if expiry is None:
            return False
        return (expiry.timestamp() - time.time()) < 60

    def token(self) -> str:
        with self._lock:
            if self._needs_refresh():
                self.credentials.refresh(self._request)
            token = getattr(self.credentials, "token", None)
            if not token:
                raise RuntimeError("Failed to obtain a Google access token.")
            return token


CANONICAL_MODEL_ID = _canonical_model_id(MODEL_ID)
BARE_MODEL_ID = CANONICAL_MODEL_ID.split("/", 1)[1]

auth = AccessTokenProvider()
OPENAI_BASE_URL = (
    f"{_vertex_host(LOCATION)}/v1/projects/{auth.project_id}/locations/{LOCATION}/"
    "endpoints/openapi"
)

app = FastAPI(title="Vertex OpenAI Proxy", version="0.1.0")


def _models_payload() -> dict[str, Any]:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": CANONICAL_MODEL_ID,
                "object": "model",
                "created": now,
                "owned_by": "google-vertex-ai",
            }
        ],
    }


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    model = _canonical_model_id(payload.get("model") or "")
    if not model:
        payload["model"] = CANONICAL_MODEL_ID
        return payload

    if model not in {CANONICAL_MODEL_ID, f"google/{BARE_MODEL_ID}"} and not ALLOW_MODEL_OVERRIDE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This proxy is pinned to model '{CANONICAL_MODEL_ID}'. "
                f"Received '{model}'."
            ),
        )
    payload["model"] = model
    # Hermes sends OpenAI stream_options.include_usage on streaming calls.
    # Vertex's OpenAI-compatible endpoint appears unstable with that field
    # in live SSE mode, so strip it before forwarding.
    if "stream_options" in payload:
        payload.pop("stream_options", None)
    return payload


def _debug_dump_payload(payload: dict[str, Any]) -> None:
    if not DEBUG_DUMP:
        return
    try:
        summary = {
            "stream": bool(payload.get("stream")),
            "model": payload.get("model"),
            "message_count": len(payload.get("messages") or []),
            "roles": [m.get("role") for m in (payload.get("messages") or []) if isinstance(m, dict)],
            "has_tools": bool(payload.get("tools")),
            "tool_count": len(payload.get("tools") or []),
            "tool_choice": payload.get("tool_choice"),
            "keys": sorted(payload.keys()),
        }
        logger.warning("Vertex payload summary: %s", summary)
        with open("/tmp/vertex_proxy_last_request.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=True)
    except Exception as exc:  # pragma: no cover - debug aid only
        logger.warning("Failed to dump debug payload: %s", exc)


def _debug_dump_vertex_request(body: dict[str, Any]) -> None:
    if not DEBUG_DUMP:
        return
    try:
        tools = body.get("tools") or []
        function_decls = []
        if tools and isinstance(tools, list):
            function_decls = (tools[0].get("functionDeclarations") or []) if isinstance(tools[0], dict) else []
        summary = {
            "content_count": len(body.get("contents") or []),
            "has_system_instruction": bool(body.get("systemInstruction")),
            "tool_decl_count": len(function_decls),
            "tool_names": [d.get("name") for d in function_decls if isinstance(d, dict)],
        }
        logger.warning("Vertex native request summary: %s", summary)
        with open("/tmp/vertex_proxy_last_vertex_request.json", "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2, ensure_ascii=True)
    except Exception as exc:  # pragma: no cover - debug aid only
        logger.warning("Failed to dump native vertex request: %s", exc)


def _sse_bytes(obj: dict[str, Any]) -> bytes:
    import json

    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n".encode("utf-8")


def _streaming_chunk(
    *,
    model: str,
    chunk_id: str,
    index: int = 0,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": index,
                "delta": delta or {},
                "finish_reason": finish_reason,
                "logprobs": None,
                "matched_stop": None,
            }
        ],
        "usage": usage or {},
    }


def _synthetic_stream_from_openai_data(data: dict[str, Any], *, fallback_model: str) -> StreamingResponse:
    model = str(data.get("model") or fallback_model or CANONICAL_MODEL_ID)
    chunk_id = str(data.get("id") or f"chatcmpl-{uuid.uuid4().hex}")
    choices = data.get("choices") or []
    choice0 = choices[0] if choices else {}
    message = choice0.get("message") or {}
    finish_reason = choice0.get("finish_reason", "stop")
    usage = data.get("usage") or {}

    async def iterator() -> AsyncIterator[bytes]:
        yield _sse_bytes(
            _streaming_chunk(
                model=model,
                chunk_id=chunk_id,
                delta={
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": message.get("reasoning_content"),
                    "tool_calls": None,
                },
                usage=usage,
            )
        )

        content = message.get("content")
        if isinstance(content, str) and content:
            yield _sse_bytes(
                _streaming_chunk(
                    model=model,
                    chunk_id=chunk_id,
                    delta={
                        "content": content,
                        "reasoning_content": None,
                        "role": None,
                        "tool_calls": None,
                    },
                    usage=usage,
                )
            )

        tool_calls = message.get("tool_calls")
        if tool_calls:
            yield _sse_bytes(
                _streaming_chunk(
                    model=model,
                    chunk_id=chunk_id,
                    delta={
                        "content": None,
                        "reasoning_content": None,
                        "role": None,
                        "tool_calls": tool_calls,
                    },
                    usage=usage,
                )
            )

        yield _sse_bytes(
            _streaming_chunk(
                model=model,
                chunk_id=chunk_id,
                delta={},
                finish_reason=finish_reason,
                usage=usage,
            )
        )
        yield b"data: [DONE]\n\n"

    return StreamingResponse(iterator(), media_type="text/event-stream")


async def _synthetic_stream_response(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> StreamingResponse:
    upstream_payload = dict(payload)
    upstream_payload["stream"] = False

    upstream = await client.post(url, headers=headers, json=upstream_payload)
    try:
        data = upstream.json()
    except ValueError:
        return Response(
            content=upstream.text,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/plain"),
        )

    if upstream.status_code >= 400:
        return JSONResponse(content=data, status_code=upstream.status_code)

    return _synthetic_stream_from_openai_data(
        data,
        fallback_model=str(payload.get("model") or CANONICAL_MODEL_ID),
    )


async def _stream_upstream(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> StreamingResponse:
    stream_ctx = client.stream("POST", url, headers=headers, json=payload)
    upstream = await stream_ctx.__aenter__()

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await stream_ctx.__aexit__(None, None, None)
        return Response(
            content=body,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    async def iterator() -> AsyncIterator[bytes]:
        saw_done = False
        try:
            async for line in upstream.aiter_lines():
                if line == "data: [DONE]":
                    saw_done = True
                if not line:
                    continue
                yield f"{line}\n\n".encode("utf-8")
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.StreamError) as exc:
            logger.warning("Upstream stream ended abruptly: %s", exc)
            if not saw_done:
                yield b"data: [DONE]\n\n"
        except Exception as exc:  # pragma: no cover - defensive stream guard
            logger.exception("Unexpected streaming error: %s", exc)
            if not saw_done:
                yield b"data: [DONE]\n\n"
        else:
            if not saw_done:
                yield b"data: [DONE]\n\n"
        finally:
            try:
                await stream_ctx.__aexit__(None, None, None)
            except Exception as exc:  # pragma: no cover - cleanup guard
                logger.warning("Ignoring upstream stream cleanup error: %s", exc)

    return StreamingResponse(
        iterator(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


def _coerce_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                pieces.append(part["text"])
        return "\n".join(pieces)
    return str(content)


def _tool_response_payload(content: str) -> dict[str, Any]:
    text = str(content or "")
    try:
        parsed = json.loads(text) if text.strip().startswith(("{", "[")) else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {"items": parsed}
    return {"output": text}


def _assistant_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_id = str(tc.get("id") or "").strip()
            fn = tc.get("function") or {}
            fn_name = str(fn.get("name") or "").strip()
            if tc_id and fn_name:
                result[tc_id] = fn_name
    return result


def _build_vertex_contents_and_system(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    tool_name_by_id = _assistant_tool_name_map(messages)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")

        if role == "system":
            text = _coerce_content_to_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role in {"tool", "function"}:
            tool_call_id = str(msg.get("tool_call_id") or msg.get("name") or "").strip()
            fn_name = tool_name_by_id.get(tool_call_id) or tool_call_id or "tool"
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": fn_name,
                                "response": _tool_response_payload(_coerce_content_to_text(msg.get("content"))),
                            }
                        }
                    ],
                }
            )
            continue

        gemini_role = "model" if role == "assistant" else "user"
        parts: list[dict[str, Any]] = []

        text = _coerce_content_to_text(msg.get("content"))
        if text:
            parts.append({"text": text})

        tool_calls = msg.get("tool_calls") or []
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "").strip()
                if not fn_name:
                    continue
                args_raw = fn.get("arguments", "")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) and args_raw else {}
                except json.JSONDecodeError:
                    args = {"_raw": str(args_raw)}
                if not isinstance(args, dict):
                    args = {"_value": args}
                parts.append({"functionCall": {"name": fn_name, "args": args}})

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    system_instruction = None
    joined = "\n".join(p for p in system_parts if p).strip()
    if joined:
        system_instruction = {"role": "system", "parts": [{"text": joined}]}
    return contents, system_instruction


def _vertex_tools_from_openai(tools: Any) -> list[dict[str, Any]]:
    def _sanitize_schema(node: Any) -> Any:
        strip_keys = {
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "minProperties",
            "maxProperties",
            "minContains",
            "maxContains",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "default",
            "examples",
            "$schema",
            "$id",
        }

        if isinstance(node, list):
            return [_sanitize_schema(item) for item in node]

        if not isinstance(node, dict):
            return node

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in strip_keys:
                continue
            cleaned = _sanitize_schema(value)
            out[key] = cleaned
        return out

    if not isinstance(tools, list) or not tools:
        return []
    decls: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        decl: dict[str, Any] = {"name": name}
        if fn.get("description"):
            decl["description"] = str(fn["description"])
        if isinstance(fn.get("parameters"), dict):
            decl["parameters"] = _sanitize_schema(fn["parameters"])
        decls.append(decl)
    return [{"functionDeclarations": decls}] if decls else []


def _vertex_tool_config_from_openai(tool_choice: Any) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"functionCallingConfig": {"mode": "AUTO"}}
        if tool_choice == "none":
            return {"functionCallingConfig": {"mode": "NONE"}}
        if tool_choice == "required":
            return {"functionCallingConfig": {"mode": "ANY"}}
    return None


def _native_vertex_url() -> str:
    return (
        f"{_vertex_host(LOCATION)}/v1/projects/{auth.project_id}/locations/{LOCATION}/"
        f"publishers/google/models/{BARE_MODEL_ID}:generateContent"
    )


def _needs_native_vertex_path(payload: dict[str, Any]) -> bool:
    if payload.get("tools"):
        return True
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in {"tool", "function"}:
            return True
        if msg.get("tool_calls"):
            return True
    return False


def _map_vertex_finish_reason(reason: str, *, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "OTHER": "stop",
    }
    return mapping.get(str(reason or "").upper(), "stop")


def _openai_usage_from_vertex(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt_tokens": int(usage.get("promptTokenCount") or 0),
        "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
        "total_tokens": int(usage.get("totalTokenCount") or 0),
        "prompt_tokens_details": None,
    }


def _openai_response_from_vertex(data: dict[str, Any], *, model: str) -> dict[str, Any]:
    candidates = data.get("candidates") or []
    cand = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    content = cand.get("content") or {}
    parts = content.get("parts") or []

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        fc = part.get("functionCall")
        if isinstance(fc, dict) and fc.get("name"):
            args = fc.get("args") or {}
            try:
                args_str = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args_str = "{}"
            tool_calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": str(fc["name"]),
                        "arguments": args_str,
                    },
                }
            )

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) if text_parts else None,
        "reasoning_content": None,
        "tool_calls": tool_calls or None,
    }
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _map_vertex_finish_reason(
                    str(cand.get("finishReason") or ""),
                    has_tool_calls=bool(tool_calls),
                ),
            }
        ],
        "usage": _openai_usage_from_vertex(data.get("usageMetadata") or {}),
    }


def _vertex_request_from_openai(payload: dict[str, Any]) -> dict[str, Any]:
    contents, system_instruction = _build_vertex_contents_and_system(payload.get("messages") or [])
    body: dict[str, Any] = {"contents": contents}
    if system_instruction:
        body["systemInstruction"] = system_instruction
    tools = _vertex_tools_from_openai(payload.get("tools"))
    if tools:
        body["tools"] = tools
    tool_config = _vertex_tool_config_from_openai(payload.get("tool_choice"))
    if tool_config:
        body["toolConfig"] = tool_config
    generation_config: dict[str, Any] = {}
    if isinstance(payload.get("temperature"), (int, float)):
        generation_config["temperature"] = float(payload["temperature"])
    if isinstance(payload.get("max_tokens"), int) and payload["max_tokens"] > 0:
        generation_config["maxOutputTokens"] = int(payload["max_tokens"])
    if isinstance(payload.get("top_p"), (int, float)):
        generation_config["topP"] = float(payload["top_p"])
    stop = payload.get("stop")
    if isinstance(stop, str) and stop:
        generation_config["stopSequences"] = [stop]
    elif isinstance(stop, list) and stop:
        generation_config["stopSequences"] = [str(s) for s in stop if s]
    if generation_config:
        body["generationConfig"] = generation_config
    return body


async def _native_vertex_openai_response(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> Response:
    vertex_body = _vertex_request_from_openai(payload)
    _debug_dump_vertex_request(vertex_body)
    upstream = await client.post(_native_vertex_url(), headers=headers, json=vertex_body)
    try:
        data = upstream.json()
    except ValueError:
        return Response(
            content=upstream.text,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/plain"),
        )
    if upstream.status_code >= 400:
        return JSONResponse(content=data, status_code=upstream.status_code)

    openai_data = _openai_response_from_vertex(
        data,
        model=str(payload.get("model") or CANONICAL_MODEL_ID),
    )
    if payload.get("stream") is True:
        return _synthetic_stream_from_openai_data(
            openai_data,
            fallback_model=str(payload.get("model") or CANONICAL_MODEL_ID),
        )
    return JSONResponse(content=openai_data, status_code=upstream.status_code)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "project_id": auth.project_id,
        "location": LOCATION,
        "model_id": CANONICAL_MODEL_ID,
        "base_url": OPENAI_BASE_URL,
    }


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return _models_payload()


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    payload = _normalize_payload(await request.json())
    _debug_dump_payload(payload)
    headers = {
        "Authorization": f"Bearer {auth.token()}",
        "Content-Type": "application/json",
    }
    url = f"{OPENAI_BASE_URL}/chat/completions"

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        if _needs_native_vertex_path(payload):
            return await _native_vertex_openai_response(client, headers, payload)

        if payload.get("stream") is True:
            if STREAM_MODE == "live":
                return await _stream_upstream(client, url, headers, payload)
            return await _synthetic_stream_response(client, url, headers, payload)

        upstream = await client.post(url, headers=headers, json=payload)
        try:
            data = upstream.json()
            return JSONResponse(content=data, status_code=upstream.status_code)
        except ValueError:
            return Response(
                content=upstream.text,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "text/plain"),
            )
