"""Active runtime capability probes: tool-calling and streaming.

States are explicit: ``supported`` | ``unsupported`` | ``unknown``
(inconclusive response shape) | ``unavailable`` (network/transport).
Probes are tiny, deterministic (forced tool choice / 1-token stream),
never send user data, and results are cached with a TTL.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

CapState = Literal["supported", "unsupported", "unknown", "unavailable"]
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class CapabilityReport:
    provider_id: str
    model: str
    protocol: str
    tool_calls: CapState = "unknown"
    streaming: CapState = "unknown"
    detail: dict[str, Any] = field(default_factory=dict)
    probed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------
# OpenAI-compatible
# ----------------------------------------------------------------------


def _openai_tool_probe(
    base_url: str,
    api_key: str,
    model: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 20.0,
) -> tuple[CapState, str]:
    """Forced tool_choice call — deterministic success/failure signal."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Call the zero_probe_tool."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "zero_probe_tool",
                    "description": "Capability probe; returns ok.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "zero_probe_tool"}},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if transport is not None:
            with httpx.Client(transport=transport) as client:
                resp = client.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.RequestError:
        return "unavailable", "network/transport error"
    if resp.status_code != 200:
        body = resp.text[:200].lower()
        if resp.status_code == 429:
            ra = resp.headers.get("retry-after")
            return (
                "unsupported" if False else "unavailable",
                f"rate limited{f' (retry_after={ra})' if ra else ''}",
            )
        if any(k in body for k in ("tool", "function", "tool_choice")):
            return "unsupported", f"http {resp.status_code}: provider rejected tools"
        return "unknown", f"http {resp.status_code}"
    try:
        msg = resp.json()["choices"][0]["message"]
    except Exception:  # noqa: BLE001 - malformed shape is inconclusive
        return "unknown", "malformed response"
    if isinstance(msg.get("tool_calls"), list) and msg["tool_calls"]:
        return "supported", ""
    return "unknown", "no tool_calls in forced probe"


def _openai_stream_probe(
    base_url: str,
    api_key: str,
    model: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 20.0,
) -> tuple[CapState, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "max_tokens": 8,
        "stream": True,
        "messages": [{"role": "user", "content": "ping"}],
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    saw_delta = False
    try:
        client_ctx = httpx.Client(transport=transport) if transport is not None else httpx.Client()
        with client_ctx.stream("POST", url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                body_snip = ""
                for chunk in resp.iter_bytes():
                    body_snip += chunk.decode("utf-8", errors="replace")[:200]
                    break
                low = body_snip.lower()
                if "stream" in low:
                    return "unsupported", f"http {resp.status_code}: {body_snip[:120]}"
                return "unknown", f"http {resp.status_code}"
            for line in resp.iter_lines():
                if not isinstance(line, str):
                    line = line.decode("utf-8", errors="replace")
                if line.startswith("data:") and "[DONE]" not in line:
                    saw_delta = True
    except httpx.RequestError:
        return "unavailable", "network/transport error"
    return (
        "supported" if saw_delta else "unknown",
        "" if saw_delta else "stream produced no delta events",
    )


# ----------------------------------------------------------------------
# Anthropic
# ----------------------------------------------------------------------


def _anthropic_tool_probe(
    base_url: str,
    api_key: str,
    model: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 20.0,
) -> tuple[CapState, str]:
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    payload = {
        "model": model,
        "max_tokens": 32,
        "tools": [
            {
                "name": "zero_probe_tool",
                "description": "Capability probe; returns ok.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }
        ],
        "tool_choice": {"type": "tool", "name": "zero_probe_tool"},
        "messages": [{"role": "user", "content": "Call the zero_probe_tool."}],
    }
    try:
        if transport is not None:
            with httpx.Client(transport=transport) as client:
                resp = client.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.RequestError:
        return "unavailable", "network/transport error"
    if resp.status_code != 200:
        body = resp.text[:200].lower()
        if resp.status_code == 429:
            return "unavailable", "rate limited"
        if any(k in body for k in ("tool", "tool_choice")):
            return "unsupported", f"http {resp.status_code}: provider rejected tools"
        return "unknown", f"http {resp.status_code}"
    try:
        blocks = resp.json().get("content", [])
        stop = resp.json().get("stop_reason")
    except Exception:  # noqa: BLE001
        return "unknown", "malformed response"
    has_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
    if has_use or stop == "tool_use":
        return "supported", ""
    return "unknown", "no tool_use block in forced probe"


def _anthropic_stream_probe(
    base_url: str,
    api_key: str,
    model: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 20.0,
) -> tuple[CapState, str]:
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    payload = {
        "model": model,
        "max_tokens": 8,
        "stream": True,
        "messages": [{"role": "user", "content": "ping"}],
    }
    saw_event = False
    try:
        client_ctx = httpx.Client(transport=transport) if transport is not None else httpx.Client()
        with client_ctx.stream("POST", url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                snip = ""
                for chunk in resp.iter_bytes():
                    snip += chunk.decode("utf-8", errors="replace")[:200]
                    break
                if "stream" in snip.lower():
                    return "unsupported", f"http {resp.status_code}"
                return "unknown", f"http {resp.status_code}"
            for line in resp.iter_lines():
                s = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
                if s.startswith("data:") and '"message_stop"' not in s:
                    saw_event = True
    except httpx.RequestError:
        return "unavailable", "network/transport error"
    return ("supported" if saw_event else "unknown", "" if saw_event else "no stream events")


# ----------------------------------------------------------------------
# Public entry + cache
# ----------------------------------------------------------------------


def probe_capabilities(
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    provider_id: str = "probe",
    transport: httpx.BaseTransport | None = None,
) -> CapabilityReport:
    report = CapabilityReport(provider_id=provider_id, model=model, protocol=protocol)
    if protocol in ("openai_compatible", "anthropic"):
        probe_fn = _openai_tool_probe if protocol == "openai_compatible" else _anthropic_tool_probe
        stream_fn = (
            _openai_stream_probe if protocol == "openai_compatible" else _anthropic_stream_probe
        )
        report.tool_calls, tc_detail = probe_fn(base_url, api_key, model, transport=transport)
        report.streaming, st_detail = stream_fn(base_url, api_key, model, transport=transport)
        if tc_detail:
            report.detail["tool_calls"] = tc_detail
        if st_detail:
            report.detail["streaming"] = st_detail
    else:
        report.tool_calls = "unknown"
        report.streaming = "unknown"
        report.detail["protocol"] = f"no probes for protocol {protocol!r}"
    return report


def _cache_key(provider_id: str, model: str, base_url: str) -> str:
    digest = hashlib.sha256(f"{provider_id}|{model}|{base_url}".encode()).hexdigest()
    return digest[:24]


class CapabilityCache:
    """TTL cache backed by ``capabilities.json`` in the config home."""

    def __init__(self, home: Path, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.path = Path(home) / "capabilities.json"
        self.ttl = ttl_seconds

    def _read_all(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, provider_id: str, model: str, base_url: str) -> CapabilityReport | None:
        entry = self._read_all().get(_cache_key(provider_id, model, base_url))
        if not entry:
            return None
        if time.time() - float(entry.get("probed_at", 0)) > self.ttl:
            return None
        return CapabilityReport(**entry)

    def put(self, report: CapabilityReport) -> None:
        data = self._read_all()
        key = _cache_key(report.provider_id, report.model, "")
        data[key] = report.to_dict()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
