"""Log redaction (ARCHITECTURE_OPS §13): exact secret values plus key-shaped patterns, as a handler-level filter.

``install(settings)`` (lifespan step 1; idempotent, re-run it after configuring new handlers) attaches one
``RedactingFilter`` to every handler of the root logger, the uvicorn loggers, every other logger that has handlers and
``logging.lastResort``, refreshes the secret values from ``settings``, and pins the ``websockets``, ``httpx`` and
``httpcore`` loggers to WARNING in every environment (the ASR handshake carries the Meta key in a text frame).
``attach(handler)`` covers a handler added later; ``redact(text)`` applies the same rewrite to any string.

Redacted: the exact value of every loaded secret (``config.SECRET_FIELDS`` and the admin tokens; values shorter than 4
characters are skipped, digit-only values only match as whole numbers) and the patterns ``LLM_<n>_…`` (Meta keys),
``Bearer …``, ``"accessToken": "…"``, ``sk_…`` (ElevenLabs; not inside a word, so ``tsk_<ulid>`` ids stay readable),
``AC`` + 32 hex (Twilio account SIDs, likewise standalone) and our own signed cookies and tokens (``eyJ….…``).
The filter rewrites the message, each argument (keeping its position, so formatters such as uvicorn's access log
still work), a message whose secret spans several arguments, the formatted traceback, the stack info and string
``extra`` fields. A filter failure drops the record's text rather than leaking it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from restaurantos.config import SECRET_FIELDS

if TYPE_CHECKING:
    from restaurantos.config import Settings

REDACTED = "[REDACTED]"
MIN_SECRET_CHARS = 4
PINNED_LOGGERS = ("websockets", "httpx", "httpcore")
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
PATTERNS: tuple[tuple[str, str], ...] = (  # (regex, replacement), tried in this order at each position
    (r'"accessToken"\s*:\s*"[^"]*"', f'"accessToken": "{REDACTED}"'),
    (r"(?i:bearer)\s+[^\s\"]+", f"Bearer {REDACTED}"),
    (r"LLM_\d+_[A-Za-z0-9_\-]{8,}", REDACTED),
    (r"(?<![A-Za-z0-9])sk_[A-Za-z0-9]{20,}", REDACTED),  # the boundary keeps ids such as tsk_<ulid> readable
    (r"(?<![A-Za-z0-9])AC[0-9a-f]{32}(?![0-9a-f])", REDACTED),
    (r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{16,}", REDACTED),  # b64url({"…) + "." + b64url(mac)
)
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}
_TRACEBACKS = logging.Formatter()


class Redactor:
    """One compiled alternation: the secret values (longest first), then ``PATTERNS``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = sorted({s for s in secrets if len(s) >= MIN_SECRET_CHARS}, key=len, reverse=True)
        exact = [rf"(?<!\d){re.escape(v)}(?!\d)" if v.isdigit() else re.escape(v) for v in values]
        parts = [f"(?P<secret>{'|'.join(exact)})"] if exact else []
        parts += [f"(?P<p{i}>{regex})" for i, (regex, _) in enumerate(PATTERNS)]
        self._regex = re.compile("|".join(parts))
        self._replacements = {"secret": REDACTED, **{f"p{i}": rep for i, (_, rep) in enumerate(PATTERNS)}}

    def redact(self, text: str) -> str:
        return self._regex.sub(lambda m: self._replacements[m.lastgroup or "secret"], text)

    def changes(self, text: str) -> bool:
        return self.redact(text) != text


def secret_values(settings: Settings) -> list[str]:
    """Every loaded secret's value: ``config.SECRET_FIELDS`` plus the admin tokens."""
    found = [getattr(settings, name, None) for name in SECRET_FIELDS] + list(settings.admin_tokens.values())
    return [s.get_secret_value() for s in found if isinstance(s, SecretStr) and s.get_secret_value()]


def _scrub_arg(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if value is None or isinstance(value, bool | int | float):
        return value  # numbers keep %d working
    text = str(value)
    if redactor.changes(text) or redactor.changes(repr(value)):
        return redactor.redact(text)
    return value


class RedactingFilter(logging.Filter):
    """Rewrites a record in place before its handler formats it; never drops a record."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._scrub(record)
        except Exception:  # a redaction bug must neither leak the text nor break the log call
            record.msg, record.args = "[log record dropped: redaction failed]", None
            record.exc_info = record.exc_text = record.stack_info = None
        return True

    def _scrub(self, record: logging.LogRecord) -> None:
        r = self.redactor
        if record.args:  # judge the rendered text: redacting the template alone could eat a placeholder
            record.args = ({k: _scrub_arg(v, r) for k, v in record.args.items()} if isinstance(record.args, Mapping)
                           else tuple(_scrub_arg(a, r) for a in record.args))
            try:
                rendered: str | None = record.getMessage()
            except Exception:  # a broken format string: handleError would print msg and args raw to stderr
                rendered = None
            if rendered is None:
                record.msg, record.args = r.redact(f"{record.msg} {record.args!r}"), None
            elif r.changes(rendered):  # a secret in the template, or spanning the template and an argument
                record.msg, record.args = r.redact(rendered), None
        elif isinstance(record.msg, str):
            record.msg = r.redact(record.msg)
        elif record.msg is not None and r.changes(str(record.msg)):
            record.msg = r.redact(str(record.msg))
        if record.exc_info and not record.exc_text:
            record.exc_text = _TRACEBACKS.formatException(record.exc_info)  # formatters reuse exc_text
        if record.exc_text:
            record.exc_text = r.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = r.redact(record.stack_info)
        for key, value in list(vars(record).items()):
            if key not in _STANDARD_ATTRS and isinstance(value, str):
                setattr(record, key, r.redact(value))


_lock = threading.Lock()
_filter: RedactingFilter | None = None
_pinned: dict[str, int] = {}  # logger name -> its level before the pin
_DEFAULT = Redactor()  # patterns only, until install() knows the secrets


def _handlers() -> Iterator[logging.Handler]:
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in UVICORN_LOGGERS),
               *(lg for lg in list(logging.Logger.manager.loggerDict.values()) if isinstance(lg, logging.Logger))]
    seen: set[int] = set()
    for handler in [h for lg in loggers for h in lg.handlers] + [logging.lastResort]:
        if handler is not None and id(handler) not in seen:
            seen.add(id(handler))
            yield handler


def install(settings: Settings) -> RedactingFilter:
    """Attach the filter everywhere and pin the chatty transport loggers; safe to call again (refreshes secrets)."""
    global _filter
    with _lock:
        redactor = Redactor(secret_values(settings))
        if _filter is None:
            _filter = RedactingFilter(redactor)
        else:
            _filter.redactor = redactor
        for handler in _handlers():
            if _filter not in handler.filters:
                handler.addFilter(_filter)
        for name in PINNED_LOGGERS:
            logger = logging.getLogger(name)
            _pinned.setdefault(name, logger.level)
            logger.setLevel(logging.WARNING)
        return _filter


def attach(handler: logging.Handler) -> None:
    """Redact a handler configured after ``install`` (P29's JSON and file handlers)."""
    if _filter is None:
        raise RuntimeError("security.redact.install(settings) has not run")
    if _filter not in handler.filters:
        handler.addFilter(_filter)


def installed() -> RedactingFilter | None:
    return _filter


def uninstall() -> None:
    """Undo ``install`` (tests): remove the filter and restore the pinned loggers' levels."""
    global _filter
    with _lock:
        if _filter is not None:
            for handler in _handlers():
                handler.removeFilter(_filter)
        for name, level in _pinned.items():
            logging.getLogger(name).setLevel(level)
        _pinned.clear()
        _filter = None


def redact(text: str) -> str:
    return (_filter.redactor if _filter is not None else _DEFAULT).redact(text)
