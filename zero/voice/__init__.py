"""Zero v2 voice/TTS package — Phase 4 (Telegram voice messages).

Telegram voice messages are Opus-encoded OGG files. The pipeline:

    1. User sends voice message → bot downloads .ogg file
    2. ``VoiceTranscriber`` transcribes (using Router or local Whisper)
    3. Text fed to agent loop as a normal user message
    4. Agent response text → ``TTSClient.synthesize()`` → Opus .ogg
    5. Bot sends via ``sendVoice`` (Telegram voice bubble)

TTS providers:
    - EdgeTTS (free, default — no API key)
    - Stub provider for tests

Per ADR 0001: aiogram 3.x — supports sendVoice natively.
"""
from __future__ import annotations

from zero.voice.tts import (
    EdgeTTSClient,
    StubTTSClient,
    TTSClient,
    TTSProvider,
    TTSResult,
    VoiceFormat,
)
from zero.voice.transcriber import (
    RouterVoiceTranscriber,
    StubVoiceTranscriber,
    TranscriptionResult,
    VoiceTranscriber,
)
from zero.voice.opus import (
    OPUS_SAMPLE_RATE,
    ensure_opus_ogg,
    is_opus_ogg,
)

__all__ = [
    "EdgeTTSClient",
    "StubTTSClient",
    "TTSClient",
    "TTSProvider",
    "TTSResult",
    "VoiceFormat",
    "RouterVoiceTranscriber",
    "StubVoiceTranscriber",
    "TranscriptionResult",
    "VoiceTranscriber",
    "ensure_opus_ogg",
    "is_opus_ogg",
    "OPUS_SAMPLE_RATE",
]
