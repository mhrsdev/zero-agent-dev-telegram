"""Text-to-Speech clients for Telegram voice messages.

Providers:
    - ``EdgeTTSClient`` — free Microsoft Edge TTS (no API key required).
      Uses the `edge-tts` Python package if available; falls back to a
      stub that returns an empty Opus file.
    - ``StubTTSClient`` — for tests; returns a deterministic placeholder.

Output format: Opus OGG (mono, 48kHz, ~32kbps) for Telegram sendVoice.
"""
from __future__ import annotations

import abc
import asyncio
import io
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from zero.voice.opus import FfmpegUnavailableError, ensure_opus_ogg, is_opus_ogg

__all__ = [
    "VoiceFormat",
    "TTSProvider",
    "TTSResult",
    "TTSClient",
    "EdgeTTSClient",
    "StubTTSClient",
    "TTSError",
    "DEFAULT_VOICE",
    "DEFAULT_RATE",
    "DEFAULT_VOLUME",
]


class TTSError(RuntimeError):
    """Raised when TTS synthesis fails."""


class VoiceFormat(str, Enum):
    """Output format for TTS synthesis."""

    OPUS_OGG = "opus_ogg"  # Telegram voice bubble
    MP3 = "mp3"            # CLI / Discord / WhatsApp
    WAV = "wav"            # raw PCM


class TTSProvider(str, Enum):
    """TTS backend identifier."""

    EDGE = "edge"
    STUB = "stub"
    OPENAI = "openai"  # extension
    ELEVENLABS = "elevenlabs"  # extension


@dataclass(frozen=True, slots=True)
class TTSResult:
    """Result of TTS synthesis."""

    audio_bytes: bytes
    format: VoiceFormat
    duration_seconds: float | None = None
    provider: TTSProvider = TTSProvider.STUB
    voice: str = ""
    # Optional metadata for billing / observability
    chars_synthesized: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "provider": self.provider.value,
            "voice": self.voice,
            "bytes": len(self.audio_bytes),
            "duration_seconds": self.duration_seconds,
            "chars": self.chars_synthesized,
        }


# Defaults — per ADR T-4.2: Opus for Telegram.
DEFAULT_VOICE = "en-US-AriaNeural"  # Microsoft Edge TTS voice
DEFAULT_RATE = "+0%"  # normal speed
DEFAULT_VOLUME = "+0%"  # normal volume


class TTSClient(abc.ABC):
    """Abstract TTS client."""

    provider: TTSProvider

    @abc.abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        rate: str | None = None,
        volume: str | None = None,
        format: VoiceFormat = VoiceFormat.OPUS_OGG,
    ) -> TTSResult:
        """Synthesize ``text`` to audio bytes.

        Args:
            text: Text to synthesize (plain text, no SSML).
            voice: Voice ID (provider-specific).
            rate: Speed adjustment (e.g. "+10%", "-5%").
            volume: Volume adjustment (e.g. "-20%").
            format: Output format.

        Returns:
            TTSResult with audio bytes.
        """
        ...


# ---------------------------------------------------------------------- EdgeTTS

class EdgeTTSClient(TTSClient):
    """Microsoft Edge TTS client (free, no API key).

    Uses the `edge-tts` Python package. If not installed, raises TTSError
    on first use.

    Per T-4.2: outputs Opus OGG for Telegram sendVoice.
    """

    provider = TTSProvider.EDGE

    def __init__(
        self,
        *,
        default_voice: str = DEFAULT_VOICE,
        default_rate: str = DEFAULT_RATE,
        default_volume: str = DEFAULT_VOLUME,
    ) -> None:
        self._default_voice = default_voice
        self._default_rate = default_rate
        self._default_volume = default_volume
        self._edge_tts: Any = None  # lazy import

    async def _ensure_edge_tts(self) -> Any:
        if self._edge_tts is None:
            try:
                import edge_tts  # type: ignore[import-not-found]  # noqa: PLC0415

                self._edge_tts = edge_tts
            except ImportError as e:
                raise TTSError(
                    "edge-tts package not installed. Install with: pip install edge-tts"
                ) from e
        return self._edge_tts

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        rate: str | None = None,
        volume: str | None = None,
        format: VoiceFormat = VoiceFormat.OPUS_OGG,
    ) -> TTSResult:
        if not text:
            raise TTSError("text must be non-empty")

        edge_tts = await self._ensure_edge_tts()
        v = voice or self._default_voice
        r = rate or self._default_rate
        vol = volume or self._default_volume

        # Edge TTS only outputs MP3 — convert to Opus if needed.
        communicate = edge_tts.Communicate(text, v, rate=r, volume=vol)
        mp3_buffer = io.BytesIO()
        try:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3_buffer.write(chunk["data"])
        except Exception as e:
            raise TTSError(f"edge-tts synthesis failed: {e}") from e

        mp3_bytes = mp3_buffer.getvalue()
        if not mp3_bytes:
            raise TTSError("edge-tts returned empty audio")

        # If caller wants MP3, return as-is.
        if format is VoiceFormat.MP3:
            return TTSResult(
                audio_bytes=mp3_bytes,
                format=format,
                provider=self.provider,
                voice=v,
                chars_synthesized=len(text),
            )

        # Convert to Opus OGG (Telegram format).
        if format is VoiceFormat.OPUS_OGG:
            opus_bytes = await _convert_to_opus(mp3_bytes, source_format="mp3")
            return TTSResult(
                audio_bytes=opus_bytes,
                format=format,
                provider=self.provider,
                voice=v,
                chars_synthesized=len(text),
            )

        if format is VoiceFormat.WAV:
            wav_bytes = await _convert_to_wav(mp3_bytes, source_format="mp3")
            return TTSResult(
                audio_bytes=wav_bytes,
                format=format,
                provider=self.provider,
                voice=v,
                chars_synthesized=len(text),
            )

        raise TTSError(f"unsupported format: {format}")


# ---------------------------------------------------------------------- Stub

class StubTTSClient(TTSClient):
    """Stub TTS client for tests — returns deterministic test bytes.

    The returned bytes are a valid (empty) OGG Opus file so that downstream
    code that checks for Opus headers succeeds.
    """

    provider = TTSProvider.STUB

    # Minimal valid OGG Opus file (just the headers — no audio frames).
    # Generated once and reused.
    _STUB_OPUS = (
        b"OggS"  # OGG capture pattern
        + bytes(252)  # rest of header page (zeroed)
        + b"OpusHead"  # Opus head magic
        + bytes(48)
        + b"OggS"  # second page
        + bytes(100)
    )

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        rate: str | None = None,
        volume: str | None = None,
        format: VoiceFormat = VoiceFormat.OPUS_OGG,
    ) -> TTSResult:
        if not text:
            raise TTSError("text must be non-empty")
        # Return a deterministic stub based on text length.
        if format is VoiceFormat.OPUS_OGG:
            audio = self._STUB_OPUS
        elif format is VoiceFormat.MP3:
            audio = b"\xff\xfb" + bytes(100)  # MP3 sync + padding
        elif format is VoiceFormat.WAV:
            audio = b"RIFF" + bytes(40)  # WAV header
        else:  # pragma: no cover  # exhaustive enum
            raise TTSError(f"unsupported format: {format}")

        # Estimate duration: 1 second per 15 chars (rough average TTS speed).
        duration = max(0.5, len(text) / 15.0)
        return TTSResult(
            audio_bytes=audio,
            format=format,
            provider=self.provider,
            voice=voice or "stub",
            duration_seconds=duration,
            chars_synthesized=len(text),
            metadata={"stub": True},
        )


# ---------------------------------------------------------------------- conversion

async def _convert_to_opus(input_bytes: bytes, *, source_format: str) -> bytes:
    """Convert audio bytes to Opus OGG via ffmpeg subprocess."""
    import tempfile  # noqa: PLC0415

    ffmpeg = await _find_ffmpeg()
    if ffmpeg is None:
        # If input is already Opus OGG, pass through.
        if is_opus_ogg(input_bytes):
            return input_bytes
        raise FfmpegUnavailableError(
            "ffmpeg not found — cannot convert to Opus. "
            "Install ffmpeg: apt install ffmpeg"
        )

    with tempfile.NamedTemporaryFile(suffix=f".{source_format}", delete=False) as in_f:
        in_f.write(input_bytes)
        in_path = Path(in_f.name)

    out_path = in_path.with_suffix(".ogg")
    try:
        result = await ensure_opus_ogg(in_path, output_path=out_path)
        return result.path.read_bytes()
    finally:
        # Cleanup.
        for p in (in_path, out_path):
            try:
                p.unlink()
            except OSError:
                pass


async def _convert_to_wav(input_bytes: bytes, *, source_format: str) -> bytes:
    """Convert audio bytes to WAV via ffmpeg subprocess."""
    import tempfile  # noqa: PLC0415

    ffmpeg = await _find_ffmpeg()
    if ffmpeg is None:
        raise FfmpegUnavailableError("ffmpeg not found — cannot convert to WAV")

    with tempfile.NamedTemporaryFile(suffix=f".{source_format}", delete=False) as in_f:
        in_f.write(input_bytes)
        in_path = Path(in_f.name)
    out_path = in_path.with_suffix(".wav")
    cmd = [
        ffmpeg, "-y", "-i", str(in_path),
        "-c:a", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    try:
        return out_path.read_bytes()
    finally:
        for p in (in_path, out_path):
            try:
                p.unlink()
            except OSError:
                pass


async def _find_ffmpeg() -> str | None:
    """Find ffmpeg binary (cached)."""
    import shutil  # noqa: PLC0415

    return shutil.which("ffmpeg")
