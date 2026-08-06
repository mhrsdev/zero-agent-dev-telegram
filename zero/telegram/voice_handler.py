"""Voice message handler for the Telegram bot.

When a user sends a voice message:
    1. Download the .ogg file via Bot API getFile + download
    2. Transcribe via VoiceTranscriber (Router or stub)
    3. Feed transcribed text to the regular message handler
    4. Synthesize the response via TTSClient (Edge TTS or stub)
    5. Send via sendVoice (Telegram voice bubble)

If TTS or transcription fails, gracefully fall back to text response.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import Message

from zero.core.logging import get_logger
from zero.core.scope import Scope
from zero.voice.opus import is_opus_ogg
from zero.voice.tts import EdgeTTSClient, StubTTSClient, TTSClient, TTSResult, VoiceFormat
from zero.voice.transcriber import (
    StubVoiceTranscriber,
    TranscriptionResult,
    VoiceTranscriber,
)

if TYPE_CHECKING:
    from zero.telegram.bot import MessageHandler
    from zero.telegram.topic_binding import ModeResolutionResult

__all__ = [
    "VoiceMessageRouter",
    "VoiceRouterConfig",
    "VoiceMessageResult",
]

_log = get_logger("zero.telegram.voice")


@dataclass(slots=True)
class VoiceRouterConfig:
    """Configuration for voice message routing."""

    enable_tts_response: bool = True
    enable_transcription: bool = True
    fallback_to_text_on_error: bool = True
    max_voice_duration_seconds: int = 300  # 5 min cap
    max_text_length_for_tts: int = 4096  # Telegram message cap


@dataclass(slots=True)
class VoiceMessageResult:
    """Result of processing a voice message."""

    transcribed_text: str = ""
    response_text: str = ""
    voice_sent: bool = False
    voice_bytes: int = 0
    transcription: TranscriptionResult | None = None
    tts: TTSResult | None = None
    error: str | None = None


class VoiceMessageRouter:
    """Routes voice messages through transcription → handler → TTS → sendVoice.

    Construction:
        >>> router = VoiceMessageRouter(
        ...     bot=bot,
        ...     transcriber=RouterVoiceTranscriber(...),
        ...     tts_client=EdgeTTSClient(),
        ...     message_handler=my_handler,
        ... )

    Usage (from aiogram message handler):
        >>> result = await router.handle_voice_message(
        ...     message=voice_message,
        ...     scope=resolved_scope,
        ...     mode_result=mode_result,
        ... )
    """

    def __init__(
        self,
        *,
        bot: Bot,
        transcriber: VoiceTranscriber,
        tts_client: TTSClient,
        message_handler: MessageHandler,
        config: VoiceRouterConfig | None = None,
    ) -> None:
        self._bot = bot
        self._transcriber = transcriber
        self._tts_client = tts_client
        self._message_handler = message_handler
        self._config = config or VoiceRouterConfig()

    async def handle_voice_message(
        self,
        *,
        message: Message,
        scope: Scope,
        mode_result: ModeResolutionResult,
        user_id: str,
        group_id: str | None,
        topic_id: int,
    ) -> VoiceMessageResult:
        """Handle an incoming voice message.

        Returns VoiceMessageResult with details for logging/audit.
        """
        result = VoiceMessageResult()

        if message.voice is None:
            result.error = "message has no voice attribute"
            return result

        # Step 1: Download the voice file.
        try:
            audio_bytes = await self._download_voice(message.voice.file_id)
        except Exception as e:
            _log.error(f"failed to download voice: {e}", exc=e)
            result.error = f"download failed: {e}"
            if self._config.fallback_to_text_on_error:
                await self._send_text(
                    message.chat.id,
                    "⚠️ Couldn't download your voice message. Please try again or send text.",
                    topic_id=topic_id,
                    reply_to=message.message_id,
                )
            return result

        # Validate it's Opus OGG (Telegram should always send this format).
        if not is_opus_ogg(audio_bytes):
            _log.warning(
                f"voice file is not Opus OGG (got {len(audio_bytes)} bytes) — attempting anyway"
            )

        # Step 2: Transcribe.
        if not self._config.enable_transcription:
            result.error = "transcription disabled"
            return result

        try:
            transcription = await self._transcriber.transcribe(audio_bytes)
        except Exception as e:
            _log.error(f"transcription failed: {e}", exc=e)
            result.error = f"transcription failed: {e}"
            if self._config.fallback_to_text_on_error:
                await self._send_text(
                    message.chat.id,
                    "⚠️ Couldn't transcribe your voice message. Please try again or send text.",
                    topic_id=topic_id,
                    reply_to=message.message_id,
                )
            return result

        result.transcribed_text = transcription.text
        result.transcription = transcription

        _log.info(
            f"voice transcribed: {transcription.to_log_dict()}",
        )

        # Step 3: Build IncomingMessage and call message handler.
        from zero.messaging import (  # noqa: PLC0415
            IncomingMessage,
            OutgoingMessage,  # noqa: F401  # re-export
            Participant,
            Platform,
        )

        incoming = IncomingMessage(
            platform=Platform.TELEGRAM,
            external_chat_id=str(message.chat.id),
            external_message_id=str(message.message_id),
            topic_id=topic_id,
            sender=Participant(
                external_id=str(message.from_user.id) if message.from_user else "0",
                display_name=(
                    message.from_user.full_name if message.from_user else "Unknown"
                ),
                is_bot=message.from_user.is_bot if message.from_user else False,
                username=message.from_user.username if message.from_user else None,
            ),
            text=transcription.text,
            scope=scope,
            raw_metadata={
                "voice_transcription": True,
                "voice_language": transcription.language,
                "voice_duration": transcription.duration_seconds,
            },
        )

        try:
            response_text = await self._message_handler(incoming, mode_result)
        except Exception as e:
            _log.error(f"message handler raised: {e}", exc=e)
            response_text = "⚠️ Internal error processing your message."

        if response_text is None:
            # Handler chose silence.
            return result

        result.response_text = response_text

        # Step 4: TTS synthesis (if enabled and response is short enough).
        if not self._config.enable_tts_response:
            # Send as text.
            await self._send_text(
                message.chat.id,
                response_text,
                topic_id=topic_id,
                reply_to=message.message_id,
            )
            return result

        if len(response_text) > self._config.max_text_length_for_tts:
            _log.info(
                f"response text too long for TTS ({len(response_text)} chars), "
                f"sending as text"
            )
            await self._send_text(
                message.chat.id,
                response_text,
                topic_id=topic_id,
                reply_to=message.message_id,
            )
            return result

        try:
            tts_result = await self._tts_client.synthesize(
                response_text,
                format=VoiceFormat.OPUS_OGG,
            )
        except Exception as e:
            _log.error(f"TTS failed: {e}", exc=e)
            # Fallback to text.
            await self._send_text(
                message.chat.id,
                response_text,
                topic_id=topic_id,
                reply_to=message.message_id,
            )
            return result

        result.tts = tts_result

        # Step 5: Send voice message.
        try:
            from aiogram.types import BufferedInputFile  # noqa: PLC0415

            voice_input = BufferedInputFile(
                file=tts_result.audio_bytes,
                filename=f"zero_response_{message.message_id}.ogg",
            )
            await self._bot.send_voice(
                chat_id=message.chat.id,
                voice=voice_input,
                message_thread_id=topic_id if topic_id > 0 else None,
                reply_to_message_id=message.message_id,
                caption=(response_text[:200] + "…" if len(response_text) > 200 else None),
            )
            result.voice_sent = True
            result.voice_bytes = len(tts_result.audio_bytes)
        except (TelegramBadRequest, TelegramNetworkError) as e:
            _log.error(f"sendVoice failed: {e}", exc=e)
            # Fallback to text.
            await self._send_text(
                message.chat.id,
                response_text,
                topic_id=topic_id,
                reply_to=message.message_id,
            )

        return result

    async def _download_voice(self, file_id: str) -> bytes:
        """Download a voice file via Bot API getFile + download."""
        file = await self._bot.get_file(file_id)
        if file.file_path is None:
            raise RuntimeError("file_path is None")
        # aiogram's download method returns bytes when given a BytesIO target.
        buf = io.BytesIO()
        await self._bot.download_file(file.file_path, buf)
        return buf.getvalue()

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        *,
        topic_id: int = 0,
        reply_to: int | None = None,
    ) -> None:
        """Fallback text sender."""
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if topic_id > 0:
            kwargs["message_thread_id"] = topic_id
        if reply_to is not None:
            kwargs["reply_to_message_id"] = reply_to
        try:
            await self._bot.send_message(**kwargs)
        except (TelegramBadRequest, TelegramNetworkError) as e:
            _log.error(f"send_message fallback failed: {e}", exc=e)


# ---------------------------------------------------------------------- factory

def make_default_voice_router(
    *,
    bot: Bot,
    message_handler: MessageHandler,
    use_stub: bool = False,
    router_base_url: str | None = None,
    router_api_key_ref: str | None = None,
    resolver: Any = None,
) -> VoiceMessageRouter:
    """Build a VoiceMessageRouter with sensible defaults.

    Args:
        bot: aiogram Bot instance.
        message_handler: The message handler to call with transcribed text.
        use_stub: If True, use Stub transcriber + TTS (for tests/dev).
        router_base_url: Router base URL (required for non-stub transcription).
        router_api_key_ref: secret:// ref to Router API key.
        resolver: SecretResolver instance.

    Returns:
        VoiceMessageRouter ready to handle voice messages.
    """
    if use_stub:
        transcriber: VoiceTranscriber = StubVoiceTranscriber()
        tts_client: TTSClient = StubTTSClient()
    else:
        if not router_base_url or not router_api_key_ref or resolver is None:
            raise ValueError(
                "router_base_url, router_api_key_ref, and resolver are required "
                "when use_stub=False"
            )
        from zero.voice.transcriber import RouterVoiceTranscriber  # noqa: PLC0415

        transcriber = RouterVoiceTranscriber(
            base_url=router_base_url,
            api_key_ref=router_api_key_ref,
            resolver=resolver,
        )
        tts_client = EdgeTTSClient()

    return VoiceMessageRouter(
        bot=bot,
        transcriber=transcriber,
        tts_client=tts_client,
        message_handler=message_handler,
    )
