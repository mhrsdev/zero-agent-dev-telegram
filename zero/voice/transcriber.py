"""Voice-to-text transcription for incoming Telegram voice messages.

Providers:
    - ``RouterVoiceTranscriber`` — uses Router's audio transcription endpoint
      (OpenAI Whisper-compatible API). Per ADR 0004: Zero is a pure HTTP
      consumer of Router; never calls OpenAI directly.
    - ``StubVoiceTranscriber`` — for tests; returns deterministic placeholder.

Input: Opus OGG bytes (Telegram voice format).
Output: ``TranscriptionResult`` with text + language + duration.
"""
from __future__ import annotations

import abc
import base64
from dataclasses import dataclass, field
from typing import Any

from zero.core.secret import SecretResolver
from zero.voice.opus import is_opus_ogg

__all__ = [
    "TranscriptionResult",
    "VoiceTranscriber",
    "RouterVoiceTranscriber",
    "StubVoiceTranscriber",
    "TranscriptionError",
]


class TranscriptionError(RuntimeError):
    """Raised when transcription fails."""


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Result of voice transcription."""

    text: str
    language: str | None = None
    duration_seconds: float | None = None
    provider: str = "stub"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "text_chars": len(self.text),
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "provider": self.provider,
        }


class VoiceTranscriber(abc.ABC):
    """Abstract voice transcriber."""

    provider: str

    @abc.abstractmethod
    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe ``audio_bytes`` to text.

        Args:
            audio_bytes: Opus OGG audio (Telegram voice format).
            language: Optional ISO 639-1 language hint (e.g. "en", "fa").
            prompt: Optional context prompt to guide transcription.

        Returns:
            TranscriptionResult.
        """
        ...


# ---------------------------------------------------------------------- Router transcriber

class RouterVoiceTranscriber(VoiceTranscriber):
    """Transcribes voice via the Router's audio transcription endpoint.

    Uses OpenAI's ``/audio/transcriptions`` API format (multipart/form-data).
    Per ADR 0004: Zero never calls OpenAI directly — Router forwards.
    """

    provider = "router"

    def __init__(
        self,
        *,
        base_url: str,
        api_key_ref: str,
        resolver: SecretResolver,
        model: str = "whisper-1",
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key_ref = api_key_ref
        self._resolver = resolver
        self._model = model
        self._timeout = timeout_seconds

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        if not audio_bytes:
            raise TranscriptionError("audio_bytes must be non-empty")

        # Resolve API key.
        try:
            secret = self._resolver.resolve(self._api_key_ref)
        except Exception as e:
            raise TranscriptionError(f"failed to resolve API key: {e}") from e

        # Build multipart form.
        import httpx  # noqa: PLC0415

        files = {"file": ("voice.ogg", audio_bytes, "audio/ogg")}
        data: dict[str, str] = {"model": self._model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt

        url = f"{self._base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {secret.reveal()}"}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, files=files, data=data, headers=headers)
        except httpx.HTTPError as e:
            raise TranscriptionError(f"HTTP error: {e}") from e

        if resp.status_code >= 400:
            raise TranscriptionError(
                f"Router returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except Exception as e:
            raise TranscriptionError(f"invalid JSON response: {e}") from e

        text = payload.get("text", "").strip()
        if not text:
            raise TranscriptionError("transcription returned empty text")

        return TranscriptionResult(
            text=text,
            language=payload.get("language") or language,
            duration_seconds=payload.get("duration"),
            provider=self.provider,
            metadata={"model": self._model, "request_id": payload.get("request_id")},
        )


# ---------------------------------------------------------------------- Stub transcriber

class StubVoiceTranscriber(VoiceTranscriber):
    """Stub transcriber for tests.

    Returns a deterministic text based on input bytes. Validates that the
    input is Opus OGG (so tests catch format issues).
    """

    provider = "stub"

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        if not audio_bytes:
            raise TranscriptionError("audio_bytes must be non-empty")

        # Validate format (but don't fail for stub — just mark in metadata).
        is_opus = is_opus_ogg(audio_bytes)

        # Deterministic stub text based on bytes length.
        text = f"[stub transcription of {len(audio_bytes)} bytes]"
        if prompt:
            text = f"[context: {prompt[:50]}] " + text

        return TranscriptionResult(
            text=text,
            language=language or "en",
            duration_seconds=min(60.0, len(audio_bytes) / 1024.0),
            provider=self.provider,
            metadata={"is_opus_ogg": is_opus, "stub": True},
        )
