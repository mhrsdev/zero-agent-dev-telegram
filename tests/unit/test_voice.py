"""Tests for the voice/TTS subsystem — Phase 4 voice messages."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
import respx

from zero.voice.opus import is_opus_ogg, ensure_opus_ogg, FfmpegUnavailableError
from zero.voice.tts import (
    EdgeTTSClient,
    StubTTSClient,
    TTSClient,
    TTSProvider,
    TTSResult,
    TTSError,
    VoiceFormat,
)
from zero.voice.transcriber import (
    RouterVoiceTranscriber,
    StubVoiceTranscriber,
    TranscriptionError,
    TranscriptionResult,
)


# ---------------------------------------------------------------------- Opus detection

class TestOpusDetection:
    def test_is_opus_ogg_detects_valid_header(self) -> None:
        # Minimal valid OGG Opus header.
        data = b"OggS" + bytes(252) + b"OpusHead" + bytes(48)
        assert is_opus_ogg(data) is True

    def test_is_opus_ogg_rejects_short_data(self) -> None:
        assert is_opus_ogg(b"Ogg") is False
        assert is_opus_ogg(b"") is False

    def test_is_opus_ogg_rejects_wrong_magic(self) -> None:
        data = b"RIFF" + bytes(100)  # WAV header
        assert is_opus_ogg(data) is False

    def test_is_opus_ogg_rejects_ogg_without_opus(self) -> None:
        # OGG but not Opus (e.g. Vorbis).
        data = b"OggS" + bytes(252) + b"vorbis" + bytes(100)
        assert is_opus_ogg(data) is False


# ---------------------------------------------------------------------- Stub TTS

class TestStubTTS:
    @pytest.mark.asyncio
    async def test_synthesize_opus_returns_bytes(self) -> None:
        client = StubTTSClient()
        result = await client.synthesize("hello world")
        assert isinstance(result, TTSResult)
        assert result.format is VoiceFormat.OPUS_OGG
        assert len(result.audio_bytes) > 0
        assert result.provider is TTSProvider.STUB
        assert result.chars_synthesized == len("hello world")
        # Duration should be reasonable.
        assert result.duration_seconds is not None
        assert result.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_synthesize_empty_text_raises(self) -> None:
        client = StubTTSClient()
        with pytest.raises(TTSError):
            await client.synthesize("")

    @pytest.mark.asyncio
    async def test_synthesize_mp3_format(self) -> None:
        client = StubTTSClient()
        result = await client.synthesize("hello", format=VoiceFormat.MP3)
        assert result.format is VoiceFormat.MP3
        assert len(result.audio_bytes) > 0

    @pytest.mark.asyncio
    async def test_synthesize_wav_format(self) -> None:
        client = StubTTSClient()
        result = await client.synthesize("hello", format=VoiceFormat.WAV)
        assert result.format is VoiceFormat.WAV

    @pytest.mark.asyncio
    async def test_duration_scales_with_text_length(self) -> None:
        client = StubTTSClient()
        short = await client.synthesize("hi")
        long = await client.synthesize("hello " * 100)
        assert long.duration_seconds > short.duration_seconds


# ---------------------------------------------------------------------- Edge TTS (without actual edge-tts package)

class TestEdgeTTS:
    @pytest.mark.asyncio
    async def test_edge_tts_raises_if_package_missing(self) -> None:
        """If edge-tts is not installed, raise TTSError on first use."""
        # Force the import to fail.
        client = EdgeTTSClient()
        client._edge_tts = None  # reset cache

        # Mock the import to raise ImportError.
        import sys  # noqa: PLC0415

        original = sys.modules.get("edge_tts")
        sys.modules["edge_tts"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(TTSError, match="edge-tts package not installed"):
                await client.synthesize("hello")
        finally:
            if original is not None:
                sys.modules["edge_tts"] = original
            else:
                sys.modules.pop("edge_tts", None)


# ---------------------------------------------------------------------- Stub transcriber

class TestStubTranscriber:
    @pytest.mark.asyncio
    async def test_transcribe_returns_text(self) -> None:
        transcriber = StubVoiceTranscriber()
        result = await transcriber.transcribe(b"OggS" + bytes(100) + b"OpusHead" + bytes(50))
        assert isinstance(result, TranscriptionResult)
        assert "stub transcription" in result.text
        assert result.provider == "stub"
        assert result.language == "en"

    @pytest.mark.asyncio
    async def test_transcribe_empty_bytes_raises(self) -> None:
        transcriber = StubVoiceTranscriber()
        with pytest.raises(TranscriptionError):
            await transcriber.transcribe(b"")

    @pytest.mark.asyncio
    async def test_transcribe_with_language_hint(self) -> None:
        transcriber = StubVoiceTranscriber()
        result = await transcriber.transcribe(b"audio bytes", language="fa")
        assert result.language == "fa"

    @pytest.mark.asyncio
    async def test_transcribe_with_prompt_context(self) -> None:
        transcriber = StubVoiceTranscriber()
        result = await transcriber.transcribe(
            b"audio bytes",
            prompt="The user is talking about Python programming",
        )
        assert "context" in result.text.lower() or "Python" in result.text


# ---------------------------------------------------------------------- Router transcriber (mocked)

class TestRouterVoiceTranscriber:
    """Router transcriber tests are covered in test_router_integration.py."""


# ---------------------------------------------------------------------- Opus conversion

class TestOpusConversion:
    @pytest.mark.asyncio
    async def test_ensure_opus_ogg_passthrough_for_already_opus(
        self, tmp_path: Path
    ) -> None:
        """If input is already Opus OGG, no conversion needed."""
        opus_data = b"OggS" + bytes(252) + b"OpusHead" + bytes(48)
        in_path = tmp_path / "input.ogg"
        in_path.write_bytes(opus_data)

        result = await ensure_opus_ogg(in_path)
        assert result.converted is False
        assert result.path.read_bytes() == opus_data

    @pytest.mark.asyncio
    async def test_ensure_opus_ogg_raises_if_ffmpeg_missing_and_not_opus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If input is not Opus AND ffmpeg is missing, raise FfmpegUnavailableError."""
        # Mock ffmpeg to be missing.
        monkeypatch.setattr("shutil.which", lambda _: None)
        in_path = tmp_path / "input.mp3"
        in_path.write_bytes(b"\xff\xfb" + bytes(100))  # fake MP3

        with pytest.raises(FfmpegUnavailableError):
            await ensure_opus_ogg(in_path)
