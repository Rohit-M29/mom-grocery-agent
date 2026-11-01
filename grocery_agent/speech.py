"""Speech in and out with Sarvam AI (Hindi, Marathi, English and mixed speech).

Audio formats: WhatsApp sends voice notes as OGG/Opus and only plays OGG/Opus
voice replies, so ffmpeg converts in both directions.
"""

from __future__ import annotations

import asyncio
import base64
import re

import httpx

from .config import Settings

SARVAM_BASE = "https://api.sarvam.ai"
TTS_CHAR_LIMIT = 1400  # stay under the per-request text limit; long replies are split
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Common Marathi words that are rare in Hindi, used only when STT gave no language.
_MARATHI_HINTS = re.compile(r"(आहे|नाही|आणि|का\s?\?|करू|पाहिजे|हवं|हवे|तुम्ही|आम्ही|झालं|चालेल|बरोबर)")


async def _ffmpeg(data: bytes, *out_args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", *out_args, "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='ignore')[:300]}")
    return out


async def to_wav16k(audio: bytes) -> bytes:
    return await _ffmpeg(audio, "-ac", "1", "-ar", "16000", "-f", "wav")


async def to_ogg_opus(audio: bytes) -> bytes:
    return await _ffmpeg(audio, "-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg")


def guess_language(text: str) -> str:
    """Pick a voice for text that did not come from speech recognition."""
    if not _DEVANAGARI.search(text):
        return "en-IN"
    return "mr-IN" if _MARATHI_HINTS.search(text) else "hi-IN"


def _split(text: str, limit: int = TTS_CHAR_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[।.!?])\s+", text):
        if len(current) + len(sentence) + 1 > limit and current:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


class Speech:
    def __init__(self, settings: Settings):
        self.s = settings
        self.http = httpx.AsyncClient(base_url=SARVAM_BASE, timeout=60,
                                      headers={"api-subscription-key": settings.sarvam_api_key})

    async def transcribe(self, audio: bytes) -> tuple[str, str | None]:
        """Returns (transcript, language code such as 'hi-IN', or None)."""
        wav = await to_wav16k(audio)
        r = await self.http.post(
            "/speech-to-text",
            files={"file": ("voice.wav", wav, "audio/wav")},
            data={"model": self.s.sarvam_stt_model, "language_code": "unknown"},
        )
        r.raise_for_status()
        body = r.json()
        return body.get("transcript", "").strip(), body.get("language_code")

    async def speak(self, text: str, language: str) -> bytes:
        """Returns OGG/Opus audio ready to send as a WhatsApp voice note."""
        if language not in {"hi-IN", "mr-IN", "en-IN"}:
            language = guess_language(text)
        wavs = []
        for chunk in _split(text):
            r = await self.http.post("/text-to-speech", json={
                "text": chunk,
                "target_language_code": language,
                "speaker": self.s.sarvam_speaker,
                "model": self.s.sarvam_tts_model,
            })
            r.raise_for_status()
            wavs.extend(base64.b64decode(a) for a in r.json()["audios"])
        if len(wavs) == 1:
            return await to_ogg_opus(wavs[0])
        # Join several WAV clips by converting each and concatenating raw PCM.
        pcm = b"".join([await _ffmpeg(w, "-ac", "1", "-ar", "22050", "-f", "s16le") for w in wavs])
        return await _ffmpeg_raw_to_ogg(pcm)

    async def aclose(self) -> None:
        await self.http.aclose()


async def _ffmpeg_raw_to_ogg(pcm: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "22050", "-ac", "1",
        "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(pcm)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='ignore')[:300]}")
    return out
