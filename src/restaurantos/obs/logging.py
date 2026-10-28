"""The logging policy (ARCHITECTURE_OPS §12-§13): JSON lines to stdout and ``<data_dir>/logs/restaurantos.jsonl``
(rotated at midnight, 7 days kept), every line with ``trace_id``; never transcripts, phone numbers or Twilio bodies.

- ``JsonFormatter``: one object per line with ``ts`` (UTC, ms), ``level``, ``logger``, ``msg``, ``trace_id`` (the
  record's, else ``trace_id_var``, else null) and the allowlisted extras only (ids, alert names, components): any other
  extra a call passes (a transcript, a form) is never printed. Tracebacks come from ``record.exc_text``, which the
  filters fill and scrub; ``exc_info`` is never formatted again.
- ``PolicyFilter``: phone numbers (E.164, ``%2B``-encoded, US formats) become ``[number]`` and the values of Twilio's
  caller fields (``From=``, ``To=``, ``Caller=``, ``Digits=``, ``SpeechResult=``, ...) ``[redacted]``, in the message,
  in each argument (positions kept, as in P11's filter, so uvicorn's access formatter still works), in the traceback
  and in string extras.
- ``install(settings, ...)`` adds the stdout and file handlers to the root logger (the file handler also to uvicorn's
  non-propagating loggers), puts ``PolicyFilter`` and P11's redaction filter (``security.redact.attach``) on every
  handler it covers, raises the root level to INFO and, given an ``AlertManager``, adds ``AlertLogBridge``. A second
  install replaces the first; ``uninstall()`` undoes it.
- ``ensure(c, alerts)`` is the lifespan path (``obs.metrics.register``, ``obs.status.run``): one install per data dir
  and alert manager, never in the ``test`` environment (tests install it themselves).

Code never logs what a guest said; ``tests/obs/test_logging.py`` also scans the source for log calls that pass
transcript-like values.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from restaurantos.contracts.common import utc_iso
from restaurantos.security import redact

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.obs.alerts import AlertManager
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

LOG_DIR = "logs"
LOG_FILE = "restaurantos.jsonl"
KEEP_DAYS = 7
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
EXTRA_KEYS = ("tenant_id", "session_id", "call_id", "plan_id", "order_id", "alert", "component", "error_class")
NUMBER, REDACTED_VALUE = "[number]", "[redacted]"
_PHONE = re.compile(
    r"(?<![\w+%])\+\s?\d[\d\s().\-]{6,18}\d"  # E.164 and spaced international forms
    r"|%2[Bb]\d{7,15}"  # a URL-encoded E.164 number (Twilio form bodies)
    r"|(?<!\d)\(\d{3}\)\s?\d{3}[\s.\-]\d{4}(?!\d)"  # (415) 555-0134
    r"|(?<![\d\-.])\d{3}[.\-]\d{3}[.\-]\d{4}(?![\d\-.])")  # 415-555-0134
_CALLER_FIELDS = (r"From|To|Caller|Called|ForwardedFrom|CallerName|Digits|SpeechResult|UnstableSpeechResult"
                  r"|(?:Caller|Called|From|To)(?:City|State|Zip|Country)")
_FORM_VALUE = re.compile(rf"\b({_CALLER_FIELDS})=([^&\s\"']*)")
_DICT_VALUE = re.compile(rf"(['\"](?:{_CALLER_FIELDS})['\"]\s*:\s*['\"])([^'\"]*)")
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}
_TRACEBACKS = logging.Formatter()

trace_id_var: ContextVar[str | None] = ContextVar("restos_trace_id", default=None)


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


def scrub(text: str) -> str:
    """``text`` without phone numbers or Twilio caller-field values."""
    text = _PHONE.sub(NUMBER, text)
    text = _FORM_VALUE.sub(lambda m: f"{m.group(1)}={REDACTED_VALUE}", text)
    return _DICT_VALUE.sub(lambda m: m.group(1) + REDACTED_VALUE, text)


@contextlib.contextmanager
def trace_context(trace_id: str | None) -> Iterator[None]:
    """Lines logged inside the block carry ``trace_id`` (unless a record names its own)."""
    token = trace_id_var.set(trace_id)
    try:
        yield
    finally:
        trace_id_var.reset(token)


def _scrub_arg(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if value is None or isinstance(value, bool | int | float):
        return value  # %d and %f keep working
    text = str(value)
    cleaned = scrub(text)
    return cleaned if cleaned != text else value


class PolicyFilter(logging.Filter):
    """Rewrites a record in place before its handler formats it (see the module docstring); never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._scrub(record)
        except Exception:  # a policy bug must neither leak the text nor break the log call
            record.msg, record.args = "[log record dropped: policy filter failed]", None
            record.exc_info = record.exc_text = record.stack_info = None
        return True

    @staticmethod
    def _scrub(record: logging.LogRecord) -> None:
        if record.args:
            if isinstance(record.args, Mapping):
                record.args = {key: _scrub_arg(value) for key, value in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub_arg(arg) for arg in record.args)
            try:
                rendered: str | None = record.getMessage()
            except Exception:
                rendered = None
            if rendered is None:
                record.msg, record.args = scrub(f"{record.msg} {record.args!r}"), None
            elif scrub(rendered) != rendered:  # a number spanning the template and an argument
                record.msg, record.args = scrub(rendered), None
        elif isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        elif record.msg is not None and scrub(str(record.msg)) != str(record.msg):
            record.msg = scrub(str(record.msg))
        if record.exc_info and not record.exc_text:
            record.exc_text = _TRACEBACKS.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        for key, value in list(vars(record).items()):
            if key not in _STANDARD and isinstance(value, str):
                setattr(record, key, scrub(value))


class JsonFormatter(logging.Formatter):
    """One JSON object per line (see the module docstring)."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        trace_id = getattr(record, "trace_id", None) or trace_id_var.get()
        out: dict[str, Any] = {"ts": utc_iso(datetime.fromtimestamp(record.created, UTC)), "level": record.levelname,
                               "logger": record.name, "msg": message,
                               "trace_id": trace_id[:64] if isinstance(trace_id, str) else None}
        for key in EXTRA_KEYS:
            value = getattr(record, key, None)
            if isinstance(value, str | int | float | bool):
                out[key] = value
        exc = record.exc_text
        if not exc and record.exc_info:  # only without the filters: scrub what we have to format ourselves
            exc = scrub(redact.redact(self.formatException(record.exc_info)))
        if exc:
            out["exc"] = exc
        if record.stack_info:
            out["stack"] = record.stack_info
        return json.dumps(out, ensure_ascii=False, default=str, separators=(",", ":"))


@dataclass
class Installed:
    added: list[tuple[logging.Logger, logging.Handler]] = field(default_factory=list)
    filtered: list[logging.Handler] = field(default_factory=list)  # handlers we only put the PolicyFilter on
    root_level: int = logging.WARNING
    log_file: Path | None = None
    data_dir: Path | None = None
    alerts: AlertManager | None = None

    def uninstall(self) -> None:
        uninstall()


_lock = threading.Lock()
_policy = PolicyFilter()
_installed: Installed | None = None


def _foreign_handlers() -> list[logging.Handler]:
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in UVICORN_LOGGERS)]
    found = [h for lg in loggers for h in lg.handlers] + [logging.lastResort]
    return list({id(h): h for h in found if h is not None}.values())


def install(settings: Settings, *, stream: TextIO | None = None, log_dir: Path | None = None,
            level: int = logging.INFO, alerts: AlertManager | None = None, clock: Callable[[], int] | None = None,
            keep_days: int = KEEP_DAYS) -> Installed:
    """Install the policy (see the module docstring) and return what was installed."""
    from restaurantos.obs.alerts import AlertLogBridge

    global _installed
    problem: str | None = None
    with _lock:
        if _installed is not None:
            _uninstall_locked()
        redact.install(settings)  # idempotent; attach() below needs it
        formatter = JsonFormatter()
        root = logging.getLogger()
        console = logging.StreamHandler(stream or sys.stdout)
        console.setFormatter(formatter)
        state = Installed(added=[(root, console)], root_level=root.level, data_dir=settings.data_dir, alerts=alerts)
        folder = log_dir or settings.data_dir / LOG_DIR
        try:
            folder.mkdir(parents=True, exist_ok=True)
            file_handler: logging.Handler | None = TimedRotatingFileHandler(
                folder / LOG_FILE, when="midnight", backupCount=keep_days, encoding="utf-8", delay=True)
        except OSError as exc:
            file_handler, problem = None, type(exc).__name__
        if file_handler is not None:
            file_handler.setFormatter(formatter)
            state.log_file = folder / LOG_FILE
            state.added.append((root, file_handler))
            state.added += [(logging.getLogger(name), file_handler) for name in UVICORN_LOGGERS
                            if not logging.getLogger(name).propagate]  # propagating ones reach root already
        if alerts is not None:
            state.added.append((root, AlertLogBridge(alerts, clock or _wall_ms)))
        for logger, handler in state.added:
            handler.addFilter(_policy)
            redact.attach(handler)
            logger.addHandler(handler)
        state.filtered = [h for h in _foreign_handlers() if _policy not in h.filters]
        for handler in state.filtered:
            handler.addFilter(_policy)
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        _installed = state
    if problem is not None:
        log.warning("obs: no log file under %s/ (%s); logging to stdout only", folder.name, problem)
    return state


def _uninstall_locked() -> None:
    global _installed
    state, _installed = _installed, None
    if state is None:
        return
    closed: set[int] = set()
    for logger, handler in state.added:
        logger.removeHandler(handler)
        if id(handler) not in closed:
            closed.add(id(handler))
            handler.close()
    for handler in state.filtered:
        handler.removeFilter(_policy)
    logging.getLogger().setLevel(state.root_level)


def uninstall() -> None:
    with _lock:
        _uninstall_locked()


def installed() -> Installed | None:
    return _installed


def ensure(c: Container, alerts: AlertManager) -> Installed | None:
    """The lifespan path: install once per data dir and alert manager; never in the ``test`` environment."""
    if c.settings.environment == "test":
        return None
    current = _installed
    if current is not None and current.data_dir == c.settings.data_dir and current.alerts is alerts:
        return current
    return install(c.settings, alerts=alerts, clock=c.clock.now_real_ms)
