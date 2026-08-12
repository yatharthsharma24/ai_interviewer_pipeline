"""Relay between the candidate's browser and Gemini Live.

    browser  ◄──ws──►  our server  ◄──ws + API key──►  Gemini Live

The relay exists because of the API key. Gemini Live authenticates with a key in the
connection URL, and there is no per-session client secret this code can hand out the way
OpenAI's ``/realtime/client_secrets`` does. Connecting the browser directly would mean
shipping the real key to every candidate — so the socket terminates here instead, and the
browser authenticates with the interview token it already holds.

Relaying has a second payoff: the transcript is captured **here**, from the upstream frames,
rather than being reported by the candidate's own page. On the OpenAI path the browser posts
its transcript back to us and a doctored page could lie about what was said. On this path it
cannot — the server saw the audio.

Audio is raw PCM in both directions (16 kHz up, 24 kHz down), base64 inside JSON frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.interview.models import Interview, Speaker
from app.interview.realtime import RealtimeError, gemini_live_url, gemini_setup_message

logger = logging.getLogger(__name__)

CLOSE_UPSTREAM_FAILED = 4001
CLOSE_NOT_CONFIGURED = 4002

_MAX_BUFFERED_CHARS = 8000


class _TurnBuffer:
    """Accumulates streamed transcription text and flushes whole turns."""

    def __init__(self) -> None:
        self._parts: dict[str, list[str]] = {"candidate": [], "interviewer": []}

    def add(self, speaker: str, text: str) -> None:
        if not text:
            return
        buffer = self._parts[speaker]
        if sum(len(p) for p in buffer) < _MAX_BUFFERED_CHARS:
            buffer.append(text)

    def flush(self, speaker: str) -> str | None:
        text = "".join(self._parts[speaker]).strip()
        self._parts[speaker] = []
        return text or None

    def flush_all(self) -> list[tuple[str, str]]:
        out = []
        for speaker in ("candidate", "interviewer"):
            text = self.flush(speaker)
            if text:
                out.append((speaker, text))
        return out


async def proxy_gemini_live(
    browser: WebSocket,
    interviews: Session,
    interview: Interview,
    *,
    record_turn,
) -> None:
    """Pump frames both ways until either side hangs up.

    ``record_turn(speaker, text)`` is called for each completed turn; it is passed in rather
    than imported so this module stays independent of the service layer's transaction rules.
    """
    try:
        import websockets
    except ImportError:  # pragma: no cover - websockets ships with uvicorn[standard]
        await browser.close(code=CLOSE_NOT_CONFIGURED, reason="websockets is not installed")
        return

    try:
        url = gemini_live_url()
    except RealtimeError as exc:
        logger.error("Gemini live not configured: %s", exc)
        await browser.close(code=CLOSE_NOT_CONFIGURED, reason=str(exc))
        return

    try:
        upstream = await websockets.connect(url, max_size=None, open_timeout=20)
    except Exception as exc:
        logger.error("Could not open the Gemini Live socket: %s", type(exc).__name__)
        await browser.close(code=CLOSE_UPSTREAM_FAILED, reason="upstream connection failed")
        return

    buffer = _TurnBuffer()

    async with upstream:
        await upstream.send(json.dumps(gemini_setup_message(interview)))

        async def browser_to_gemini() -> None:
            while True:
                message = await browser.receive_text()
                await upstream.send(message)

        async def gemini_to_browser() -> None:
            async for raw in upstream:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue

                _harvest_transcript(frame, buffer, record_turn)

                await browser.send_text(raw)

        pump_up = asyncio.create_task(browser_to_gemini())
        pump_down = asyncio.create_task(gemini_to_browser())

        try:
            done, pending = await asyncio.wait(
                {pump_up, pump_down}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    logger.info("Live relay ended: %s: %s", type(exc).__name__, exc)
        finally:
            for speaker, text in buffer.flush_all():
                record_turn(speaker, text)


def _harvest_transcript(frame: dict, buffer: _TurnBuffer, record_turn) -> None:
    """Pull transcription text out of one Gemini Live server frame.

    Shapes handled, per the Live API's ``BidiGenerateContentServerContent``:
      ``serverContent.inputTranscription.text``   — what the candidate said
      ``serverContent.outputTranscription.text``  — what the interviewer said
      ``serverContent.turnComplete``              — flush the interviewer's turn
      ``serverContent.generationComplete``        — same, older field name

    Unknown shapes are ignored rather than raised on: a transcript row is worth less than
    the call staying up.
    """
    content = frame.get("serverContent")
    if not isinstance(content, dict):
        return

    inbound = content.get("inputTranscription")
    if isinstance(inbound, dict):
        buffer.add("candidate", inbound.get("text", ""))

    outbound = content.get("outputTranscription")
    if isinstance(outbound, dict):
        buffer.add("interviewer", outbound.get("text", ""))

    if content.get("interrupted") or content.get("turnComplete") or content.get("generationComplete"):
        for speaker in ("candidate", "interviewer"):
            text = buffer.flush(speaker)
            if text:
                record_turn(speaker, text)


VALID_SPEAKERS = {Speaker.candidate.value, Speaker.interviewer.value}
