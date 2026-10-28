"""A local Twilio for the phone path (CONTRACTS_API §6, P27b): one call against a running app, or in-process in CI.

1. ``POST /twilio/voice``, signed like Twilio's with the app's ``twilio_auth_token`` (read from Settings, never
   printed), and the stream token read back from the ``<Parameter name="t">`` of its ``<Connect><Stream>``;
2. the media socket: ``connected``, ``start`` with the token, then the caller's audio as 20 ms ``media`` frames (a WAV
   resampled to 8 kHz μ-law, else a tone) and silence after it, in real time or ``--fast``;
3. each ``mark`` the app sends is echoed once the audio before it has played (at once with ``--fast``), as Twilio does;
   after the ``--dtmf-after``-th mark (2: the greeting, then the reply to the caller) the ``--dtmf`` keys go out;
4. the call ends when the app closes the socket (after its goodbye), or after ``--max-s`` with a ``stop``. The app's
   audio is written to ``--out`` as an 8 kHz WAV.

With ``RESTOS_FAKE_ASR=1`` (dev) the app hears the fake ASR's script, whose first turn orders two chicken biryanis and a
paneer butter masala, so ``--dtmf 1`` places the order once the read-back has a voice (live TTS, or stitched from the
pre-rendered phrase cache). Without either (no ElevenLabs key, no ``build_phrase_cache.py --apply``) the read-back is
200 ms of silence, which never confirms an order (P1-1): the app says ``degraded_phone`` and ends the call. CI runs
the same ``run_call`` in-process with the fake ASR and a fake voice (``tests/voice/test_twilio_media.py``).

Usage:
    .venv/bin/python scripts/twilio_sim.py --target http://127.0.0.1:8000 --fast --out /tmp/call.wav
    .venv/bin/python scripts/twilio_sim.py --target http://127.0.0.1:8000 --wav data/eval_audio/probes/en_order.wav

``--public-base-url`` must be the app's ``RESTOS_PUBLIC_BASE_URL`` (the URL Twilio signs). Exit codes: 0 the app ended
the call, 1 it refused or never ended it, 2 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
import numpy as np

from restaurantos.audio import pcm16_to_ulaw, read_wav, resample, ulaw_to_pcm16, write_wav
from restaurantos.security.auth import twilio_signature

FRAME, FRAME_S = 160, 0.02  # 20 ms of μ-law at 8 kHz
SILENT = b"\xff" * FRAME
TOKEN_PARAM = re.compile(r'<Parameter name="t" value="([^"]+)"')
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


class Transport(Protocol):
    """One media socket from Twilio's side."""

    async def send(self, message: dict[str, Any]) -> None: ...
    async def recv(self) -> dict[str, Any] | None: ...  # None once the app closed the socket
    async def close(self) -> None: ...


@dataclass
class SimResult:
    marks: list[str] = field(default_factory=list)
    outbound: bytearray = field(default_factory=bytearray)  # the app's audio, μ-law 8 kHz
    clears: int = 0
    dtmf_sent: str = ""
    closed_by_app: bool = False
    seconds: float = 0.0

    def wav(self) -> bytes:
        return write_wav(ulaw_to_pcm16(np.frombuffer(bytes(self.outbound), dtype=np.uint8)), 8_000)


def tone_ulaw(ms: int, hz: float = 440.0) -> bytes:
    """``ms`` of a tone as μ-law 8 kHz: speech to the fake ASR (it hears any non-silent audio as words)."""
    return pcm16_to_ulaw((8_000 * np.sin(2 * np.pi * hz * np.arange(8 * ms) / 8_000)).astype(np.int16)).tobytes()


def wav_ulaw(data: bytes) -> bytes:
    """A WAV (any rate, mono or not) as μ-law 8 kHz."""
    samples, rate = read_wav(data)
    pcm = resample(samples, rate, 8_000) if rate != 8_000 else samples
    return pcm16_to_ulaw(np.clip(np.round(pcm), -32_768, 32_767).astype(np.int16)).tobytes()


def new_call_sid() -> str:
    return "CA" + secrets.token_hex(16)


async def fetch_token(http: httpx.AsyncClient, *, public_base_url: str, auth_token: str, call_sid: str, frm: str,
                      to: str) -> str:
    """``POST /twilio/voice`` signed as Twilio signs it; the stream token (ValueError when the app did not stream)."""
    form = {"CallSid": call_sid, "AccountSid": "AC" + "0" * 32, "From": frm, "To": to, "CallStatus": "ringing",
            "Direction": "inbound", "ApiVersion": "2010-04-01"}
    signature = twilio_signature(auth_token, public_base_url.rstrip("/") + "/twilio/voice", form)
    response = await http.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": signature})
    response.raise_for_status()
    if (found := TOKEN_PARAM.search(response.text)) is None:
        raise ValueError("the app did not answer with a stream (rejected, or asking for a code)")
    return found.group(1)


async def run_call(transport: Transport, *, token: str, call_sid: str, audio: bytes, dtmf: str = "1",
                   dtmf_after: int = 2, fast: bool = False, tail_ms: int = 1_500, max_s: float = 60.0) -> SimResult:
    """One call over ``transport`` (steps 2-4 of the module docstring)."""
    result, stream, loop = SimResult(), "MZ" + secrets.token_hex(16), asyncio.get_running_loop()

    async def send(event: str, **body: Any) -> None:
        await transport.send({"event": event, "streamSid": stream, **body})

    await transport.send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    await send("start", start={"accountSid": "AC" + "0" * 32, "streamSid": stream, "callSid": call_sid,
                               "tracks": ["inbound"], "customParameters": {"t": token},
                               "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}})
    t0 = loop.time()

    async def talk() -> None:  # the caller; Twilio's stream never pauses, so silence follows in real time
        speech = audio + b"\xff" * (8 * tail_ms)
        frames = [speech[i:i + FRAME].ljust(FRAME, b"\xff") for i in range(0, len(speech), FRAME)]
        n = 0
        while n < len(frames) or not fast:
            if not fast:
                await asyncio.sleep(max(0.0, t0 + n * FRAME_S - loop.time()))
            frame = frames[n] if n < len(frames) else SILENT
            await send("media", media={"track": "inbound", "payload": base64.b64encode(frame).decode("ascii")})
            n += 1

    async def listen() -> None:  # the app; a mark returns once the audio before it has played
        played = loop.time()
        while True:
            try:
                message = await transport.recv()
            except TimeoutError:
                continue
            if message is None:
                break
            if message.get("event") == "media":
                chunk = base64.b64decode(message["media"]["payload"])
                result.outbound += chunk
                played = max(played, loop.time()) + len(chunk) / 8_000
            elif message.get("event") == "clear":
                result.clears, played = result.clears + 1, loop.time()
            elif message.get("event") == "mark":
                if not fast:
                    await asyncio.sleep(max(0.0, played - loop.time()))
                result.marks.append(message["mark"]["name"])
                await send("mark", mark={"name": message["mark"]["name"]})
                if len(result.marks) == dtmf_after and dtmf:
                    for digit in dtmf:
                        await send("dtmf", dtmf={"track": "inbound_track", "digit": digit})
                    result.dtmf_sent = dtmf
        result.closed_by_app = True

    talker, listener = asyncio.create_task(talk()), asyncio.create_task(listen())
    try:
        await asyncio.wait_for(asyncio.shield(listener), max_s)
    except TimeoutError:
        with contextlib.suppress(Exception):
            await send("stop", stop={"accountSid": "AC" + "0" * 32, "callSid": call_sid})
            await transport.close()
    finally:
        for task in (talker, listener):
            task.cancel()
        await asyncio.gather(talker, listener, return_exceptions=True)
    result.seconds = loop.time() - t0
    return result


class WebsocketTransport:
    """``Transport`` over a real socket (``websockets``)."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(message))

    async def recv(self) -> dict[str, Any] | None:
        from websockets.exceptions import ConnectionClosed

        try:
            raw = await self.ws.recv()
        except ConnectionClosed:
            return None
        return json.loads(raw) if isinstance(raw, str) else {}

    async def close(self) -> None:
        await self.ws.close()


def media_url(target: str) -> str:
    parts = urlsplit(target)
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, "/twilio/media", "", ""))


async def place_call(args: argparse.Namespace, *, public_base_url: str, auth_token: str, to: str) -> int:
    from websockets.asyncio.client import connect

    audio = wav_ulaw(args.wav.read_bytes()) if args.wav else tone_ulaw(args.tone_ms)
    call_sid = new_call_sid()
    try:
        async with httpx.AsyncClient(base_url=args.target, timeout=10.0) as http:
            token = await fetch_token(http, public_base_url=public_base_url, auth_token=auth_token,
                                      call_sid=call_sid, frm=args.caller, to=to)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"the app refused the call: {type(exc).__name__}: {exc}")
        return EXIT_FAIL
    async with connect(media_url(args.target), max_size=2**20) as ws:
        result = await run_call(WebsocketTransport(ws), token=token, call_sid=call_sid, audio=audio, dtmf=args.dtmf,
                                dtmf_after=args.dtmf_after, fast=args.fast, max_s=args.max_s)
    if args.out is not None:
        args.out.write_bytes(result.wav())
    print(f"{len(result.marks)} utterance(s), {len(result.outbound) / 8_000:.1f} s of audio from the app, keys "
          f"{result.dtmf_sent or 'none'}; {'the app ended the call' if result.closed_by_app else 'no end'} after "
          f"{result.seconds:.1f} s")
    return EXIT_OK if result.closed_by_app else EXIT_FAIL


def main(argv: list[str] | None = None) -> int:
    from restaurantos.config import Settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", default="http://127.0.0.1:8000", help="where the app listens")
    parser.add_argument("--public-base-url", help="the app's RESTOS_PUBLIC_BASE_URL (default: from Settings)")
    parser.add_argument("--wav", type=Path, help="the caller's audio (default: a tone)")
    parser.add_argument("--tone-ms", type=int, default=1_200)
    parser.add_argument("--dtmf", default="1", help="keys sent after the --dtmf-after-th utterance")
    parser.add_argument("--dtmf-after", type=int, default=2)
    parser.add_argument("--caller", default="+15085550100", help="the From number (a test number)")
    parser.add_argument("--to", help="one of the app's numbers (default: the public lane's)")
    parser.add_argument("--fast", action="store_true", help="no real-time pacing")
    parser.add_argument("--max-s", type=float, default=60.0)
    parser.add_argument("--out", type=Path, help="write the app's audio here as an 8 kHz WAV")
    args = parser.parse_args(argv)
    settings = Settings()
    token = settings.twilio_auth_token.get_secret_value() if settings.twilio_auth_token is not None else ""
    to = args.to or settings.phone_numbers.get("public")
    if not token or not to:
        print("usage: needs the app's Twilio auth token and a phone number (RESTOS_TWILIO_AUTH_TOKEN and "
              "RESTOS_PHONE_NUMBERS, or the twilio_credentials file in dev; or --to)")
        return EXIT_USAGE
    if args.wav is not None and not args.wav.is_file():
        print(f"usage: no WAV at {args.wav}")
        return EXIT_USAGE
    base = args.public_base_url or settings.public_base_url
    return asyncio.run(place_call(args, public_base_url=base, auth_token=token, to=to))


if __name__ == "__main__":
    sys.exit(main())
