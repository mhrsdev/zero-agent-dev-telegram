"""Opus OGG audio utilities for Telegram voice messages.

Telegram requires voice messages as OGG Opus files (mono, ~16-32kbps).

This module wraps ffmpeg subprocess calls. If ffmpeg is not available,
we fall back to a passthrough that assumes the input is already Opus.
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "OPUS_SAMPLE_RATE",
    "OPUS_BITRATE",
    "ensure_opus_ogg",
    "is_opus_ogg",
    "FfmpegUnavailableError",
]


OPUS_SAMPLE_RATE = 48000  # Opus native sample rate
OPUS_BITRATE = "32k"  # voice-quality bitrate


class FfmpegUnavailableError(RuntimeError):
    """Raised when ffmpeg is not installed and the audio needs conversion."""


@dataclass(slots=True)
class OpusResult:
    """Result of an Opus conversion."""

    path: Path
    bytes_size: int
    duration_seconds: float | None = None
    converted: bool = False  # True if ffmpeg was actually invoked


def is_opus_ogg(data: bytes) -> bool:
    """Quick check: does ``data`` look like an OGG Opus file?

    OGG pages start with ``OggS`` magic. Opus streams have ``OpusHead`` in
    the second page header. We just check for both signatures.
    """
    if len(data) < 64:
        return False
    if data[:4] != b"OggS":
        return False
    # Look for OpusHead in the first 4KB.
    return b"OpusHead" in data[:4096]


async def ensure_opus_ogg(
    input_path: Path,
    *,
    output_path: Path | None = None,
    bitrate: str = OPUS_BITRATE,
    sample_rate: int = OPUS_SAMPLE_RATE,
) -> OpusResult:
    """Ensure ``input_path`` is a valid Opus OGG file.

    If the input is already Opus OGG, returns immediately (no conversion).
    Otherwise invokes ffmpeg to transcode.

    Raises :class:`FfmpegUnavailableError` if ffmpeg is missing AND input
    is not already Opus.

    Args:
        input_path: Source audio file (any ffmpeg-supported format).
        output_path: Destination path. If None, writes to ``input_path.with_suffix('.ogg')``.
        bitrate: Opus bitrate (e.g. "32k", "64k").
        sample_rate: Sample rate (Opus native is 48000).

    Returns:
        OpusResult with the path to the Opus file.
    """
    if output_path is None:
        output_path = input_path.with_suffix(".ogg")

    # Check if input is already Opus OGG.
    try:
        data = input_path.read_bytes()
    except OSError as e:
        raise FfmpegUnavailableError(f"cannot read input file {input_path}: {e}") from e

    if is_opus_ogg(data) and output_path == input_path:
        return OpusResult(
            path=output_path,
            bytes_size=len(data),
            converted=False,
        )

    # Need to convert — check ffmpeg availability.
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        if is_opus_ogg(data):
            # Already Opus — just copy to output_path.
            if output_path != input_path:
                output_path.write_bytes(data)
            return OpusResult(
                path=output_path,
                bytes_size=len(data),
                converted=False,
            )
        raise FfmpegUnavailableError(
            f"ffmpeg not found in PATH — cannot convert {input_path} to Opus. "
            "Install ffmpeg: apt install ffmpeg  /  brew install ffmpeg"
        )

    # Run ffmpeg subprocess.
    cmd = [
        ffmpeg_path,
        "-y",  # overwrite output
        "-i", str(input_path),
        "-c:a", "libopus",
        "-b:a", bitrate,
        "-ar", str(sample_rate),
        "-ac", "1",  # mono (voice)
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise FfmpegUnavailableError(
            f"ffmpeg failed (exit {proc.returncode}): {stderr.decode('utf-8', errors='replace')[:500]}"
        )

    # Probe duration via ffprobe (optional).
    duration = await _probe_duration(output_path)

    return OpusResult(
        path=output_path,
        bytes_size=output_path.stat().st_size,
        duration_seconds=duration,
        converted=True,
    )


async def _probe_duration(path: Path) -> float | None:
    """Get audio duration in seconds via ffprobe (best-effort)."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            text = stdout.decode("utf-8", errors="replace").strip()
            return float(text) if text else None
    except (OSError, ValueError):
        pass
    return None
