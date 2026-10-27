"""P11 redaction: exact secret values and key-shaped patterns, as a handler-level filter on the root and uvicorn
handlers, with the websockets, httpx and httpcore loggers pinned to WARNING (ARCHITECTURE_OPS §13)."""

from __future__ import annotations

import json
import logging

import pytest
from uvicorn.logging import AccessFormatter
from websockets.client import ClientProtocol
from websockets.frames import Frame
from websockets.uri import parse_uri
from websockets.utils import accept_key

from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.ids import new_id
from restaurantos.meta.asr_realtime import RealtimeASR
from restaurantos.security import redact
from tests.fakes.container import make_test_container, make_test_settings

META_KEY = "LLM_1234_" + "Zq9_x-" * 8
TWILIO_SID = "AC" + "0123456789abcdef" * 2
TWILIO_TOKEN = "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b"
ELEVEN_KEY = "sk_" + "a1B2c3D4" * 6
SECRETS = {
    "model_api_key": META_KEY, "twilio_account_sid": TWILIO_SID, "twilio_auth_token": TWILIO_TOKEN,
    "elevenlabs_api_key": ELEVEN_KEY, "session_secret": "session-secret-0123456789abcdef",
    "judge_passcode": "spicy-route-2026", "ops_token": "ops-token-0123456789", "demo_dtmf_code": "482913",
}
ADMIN = {"vamsi": "vamsi-admin-token-000111222", "akshitha": "akshitha-admin-token-333444555"}
EVERY_SECRET = [*SECRETS.values(), *ADMIN.values()]


class Capture(logging.Handler):
    def __init__(self, formatter: logging.Formatter | None = None) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(formatter or logging.Formatter("%(levelname)s %(name)s %(message)s"))
        self.lines: list[str] = []
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def logs():
    """A root handler capturing everything, and a clean slate for the pins and levels afterwards."""
    root = logging.getLogger()
    capture = Capture()
    levels = {name: logging.getLogger(name).level for name in ("", "websockets", "httpx", "httpcore", "restaurantos")}
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        redact.uninstall()
        root.removeHandler(capture)
        for name, level in levels.items():
            logging.getLogger(name or None).setLevel(level)


def secret_settings(**extra):
    return make_test_settings(**SECRETS, admin_tokens=ADMIN, **extra)


def test_redact_llm_key_bearer_twilio_elevenlabs(logs):
    settings = secret_settings()
    access = logging.getLogger("uvicorn.access")  # configured the way uvicorn does: its own handler, no propagation
    access_capture = Capture(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    access.addHandler(access_capture)
    access.propagate, access.level = False, logging.INFO
    try:
        assert redact.install(settings) is redact.install(settings)  # idempotent: one filter, refreshed
        assert sum(isinstance(f, redact.RedactingFilter) for f in logs.filters) == 1
        assert redact.installed() in access_capture.filters  # uvicorn's handlers too
        log = logging.getLogger("restaurantos.test_redact")
        log.info("meta key %s and %r", META_KEY, META_KEY)
        log.info("Authorization: Bearer %s", "opaque-token-not-a-known-secret")  # the secret spans two pieces
        log.info("handshake %s", {"authorization": {"accessToken": f"Bearer {META_KEY}"}})
        log.info('raw frame {"accessToken": "anything at all", "mode": "PUSH_TO_TALK"}')
        log.warning("twilio sid=%s token=%s", TWILIO_SID, TWILIO_TOKEN)
        log.error("tts failed with key %s", ELEVEN_KEY)
        log.info("other shapes: LLM_77_abcdefgh12 sk_ZZZZZZZZZZZZZZZZZZZZZZZZ AC%s", "f" * 32)
        log.info("passcode link /?k=%s ops=%s secret=%s", SECRETS["judge_passcode"], SECRETS["ops_token"],
                 SECRETS["session_secret"])
        log.info("admin %(name)s used %(token)s", {"name": "vamsi", "token": ADMIN["vamsi"]})
        log.info("demo code %s armed; unrelated numbers 94829130 and 1793113200000 stay", SECRETS["demo_dtmf_code"])
        log.info("cookie ros_sess=eyJsaWQiOiJ4In0.c2lnbmF0dXJlLXNpZ25hdHVyZQ seen")
        log.info("extra fields too", extra={"auth_header": f"Bearer {ELEVEN_KEY}", "count": 3})
        try:
            raise RuntimeError(f"upstream refused {META_KEY}")
        except RuntimeError:
            log.exception("call failed")
        access.info('%s - "%s %s HTTP/%s" %d', "203.0.113.7:5000", "GET", f"/?k={SECRETS['judge_passcode']}", "1.1",
                     200)
    finally:
        access.removeHandler(access_capture)
        access.propagate, access.level = True, logging.NOTSET
    text = logs.text + "\n" + access_capture.text
    words = [w.strip(".,;:'\"=()/?") for w in text.replace("=", " ").split()]
    for secret in EVERY_SECRET + ["opaque-token-not-a-known-secret", "LLM_77_abcdefgh12", "f" * 32,
                                  "sk_ZZZZZZZZZZZZZZZZZZZZZZZZ", "anything at all", "c2lnbmF0dXJlLXNpZ25hdHVyZQ"]:
        if secret.isdigit():  # a digit-only secret is matched as a whole number, never inside a longer one
            assert secret not in words, secret
        else:
            assert secret not in text, secret
    assert "demo code [REDACTED] armed; unrelated numbers 94829130 and 1793113200000 stay" in text
    assert "Authorization: Bearer [REDACTED]" in text and '"accessToken": "[REDACTED]"' in text
    assert '"mode": "PUSH_TO_TALK"' in text and "admin vamsi used [REDACTED]" in text
    assert "RuntimeError: upstream refused [REDACTED]" in text  # the traceback is redacted too
    assert access_capture.lines == ['203.0.113.7:5000 - "GET /?k=[REDACTED] HTTP/1.1" 200 OK']
    extra = next(r for r in logs.records if r.getMessage() == "extra fields too")
    assert extra.auth_header == "Bearer [REDACTED]" and extra.count == 3
    assert redact.redact(f"x {META_KEY} y") == "x [REDACTED] y"
    redact.uninstall()
    assert redact.redact(f"x {META_KEY} y") == "x [REDACTED] y"  # the patterns alone still catch the key shape
    assert redact.redact("vamsi-admin-token-000111222") == "vamsi-admin-token-000111222"  # exact values need install


def test_key_shapes_inside_ids_stay_readable(logs):
    """A staff-task id ``tsk_<ulid>`` holds ``sk_`` + 26 characters: the ElevenLabs and Twilio shapes only match on
    their own (as the .gitleaks.toml rules do), while every standalone key is still scrubbed."""
    task = new_id("tsk")
    assert redact.redact(f"task {task} blocked") == f"task {task} blocked"
    assert redact.redact(f"admin {ELEVEN_KEY} and task {task}") == f"admin [REDACTED] and task {task}"
    for shape in (ELEVEN_KEY, f"xi-api-key:{ELEVEN_KEY}", f"key={ELEVEN_KEY}", f'"{ELEVEN_KEY}"', f"my_{ELEVEN_KEY}"):
        assert ELEVEN_KEY not in redact.redact(shape), shape
    flac = "FLAC" + "ab" * 16
    assert redact.redact(f"{flac} sid {TWILIO_SID}") == f"{flac} sid [REDACTED]"
    assert redact.redact(f"/Accounts/{TWILIO_SID}/Calls.json") == "/Accounts/[REDACTED]/Calls.json"
    redact.install(secret_settings())  # the installed filter: exact values plus the same patterns
    logging.getLogger("restaurantos.test_redact").info("admin %s and task %s", ADMIN["vamsi"], task)
    assert logs.lines[-1].endswith(f"admin [REDACTED] and task {task}")


def test_debug_logging_of_asr_handshake_redacted(logs, monkeypatch):
    settings = secret_settings()
    c = make_test_container(settings=settings)
    asr = RealtimeASR(settings, c.guard, ctx=CallCtx.dev("P11", trace_id="tr_test"), mode="PUSH_TO_TALK")
    frame = json.dumps(asr.handshake())
    assert META_KEY in frame  # the handshake really carries the key in a text frame
    monkeypatch.setattr(Frame, "MAX_LOG_SIZE", 100_000)  # WEBSOCKETS_MAX_LOG_SIZE: frames logged in full
    for name in ("websockets", "httpx", "httpcore"):  # a developer turned DEBUG on everywhere
        logging.getLogger(name).setLevel(logging.DEBUG)

    def handshake_and_send() -> None:
        """The client side of the ASR socket, sans I/O: the opening handshake, then the auth frame."""
        protocol = ClientProtocol(parse_uri(f"{settings.meta_asr_ws_url}?sessionId={asr.asr_session_id}"))
        request = protocol.connect()
        protocol.send_request(request)
        protocol.receive_data(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                              b"Sec-WebSocket-Accept: " + accept_key(request.headers["Sec-WebSocket-Key"]).encode()
                              + b"\r\n\r\n")
        protocol.send_text(frame.encode())

    redact.install(settings)
    for name in ("websockets", "httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING  # pinned in every environment
    assert not logging.getLogger("httpcore.http11").isEnabledFor(logging.DEBUG)
    handshake_and_send()
    assert not any(r.name.startswith("websockets") for r in logs.records)  # the pin silences the frame log

    # even if someone lifts the pin later, the handler filter still redacts the frame
    logging.getLogger("websockets").setLevel(logging.DEBUG)
    handshake_and_send()
    logging.getLogger("restaurantos.meta.asr_realtime").debug("handshake frame %s", frame)
    frames = [line for line in logs.lines if "TEXT" in line or "handshake frame" in line]
    assert len(frames) == 2 and all('"accessToken": "[REDACTED]"' in line for line in frames)
    assert META_KEY not in logs.text and "Bearer LLM_" not in logs.text
    assert any("> GET /v1/asr/realtime" in line for line in logs.lines)  # the rest of the debug log is intact

    redact.uninstall()
    assert logging.getLogger("websockets").level == logging.DEBUG  # uninstall restores the levels it pinned


def test_filter_reaches_late_handlers_and_never_leaks_on_failure(logs):
    with pytest.raises(RuntimeError):
        redact.attach(Capture())  # nothing to attach before install
    settings = secret_settings()
    redact.install(settings)
    assert redact.installed() in logging.lastResort.filters  # records with no handler at all are covered too
    logger = logging.getLogger("restaurantos.test_redact.late")
    late = Capture()
    logger.addHandler(late)
    logger.propagate = False
    try:
        logger.warning("late handler sees %s", "LLM_1_" + "q" * 10)
        assert "LLM_1_qqqqqqqqqq" in late.text  # a handler added after install is covered only once attached
        redact.install(settings)  # re-running install (or attach) covers it
        logger.warning("now it sees %s", META_KEY)
        assert late.lines[-1].endswith("now it sees [REDACTED]")
        other = Capture()
        redact.attach(other)
        redact.attach(other)  # idempotent
        assert other.filters.count(redact.installed()) == 1
        logger.addHandler(other)

        class Hostile:
            def __str__(self) -> str:
                raise ValueError(META_KEY)

        logger.warning("value %s", Hostile())
        assert other.lines == ["WARNING restaurantos.test_redact.late [log record dropped: redaction failed]"]
    finally:
        logger.handlers.clear()
        logger.propagate = True
