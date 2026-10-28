"""Point the Twilio number(s) at a deployment (ARCHITECTURE §3, CONTRACTS_API §6, ARCHITECTURE_OPS restore runbook).

For each configured number (one number serving both lanes is updated once) it sets:
- the voice URL ``<target>/twilio/voice`` (POST);
- the status callback ``<target>/twilio/status`` (POST);
- the voice fallback, which Twilio fetches when the app cannot answer: TwiML that says the ``busy`` line and hangs
  up. ``--fallback-url`` names a TwiML Bin holding the printed TwiML; without it, Twilio's Echo twimlet serves that
  same TwiML.

Usage:
    .venv/bin/python scripts/twilio_setup.py --target https://<name>.trycloudflare.com      # dry run (the default)
    .venv/bin/python scripts/twilio_setup.py --target https://restaurantos.fly.dev --apply  # the lead, founder OK

``--target`` defaults to ``RESTOS_PUBLIC_BASE_URL`` and must be an https origin: Media Streams need ``wss://`` and the
app checks Twilio's signatures against its own ``public_base_url``, so the two must match. Credentials and numbers come
only from Settings (``RESTOS_TWILIO_*`` and ``RESTOS_PHONE_NUMBERS``, or in dev the gitignored ``twilio_credentials``
file) and are never printed: the account SID shows as ``AC…1234`` and numbers as ``…1967``. A dry run makes no network
call.

Exit codes: 0 done, 1 Twilio refused a request or a number is not on the account, 2 usage (a non-https target, no
number configured, or ``--apply`` without credentials).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape

import httpx

from restaurantos.config import Settings
from restaurantos.security.redact import Redactor
from restaurantos.voice import numbers
from restaurantos.voice.twilio_http import say_text

API = "https://api.twilio.com/2010-04-01"
ECHO_TWIMLET = "https://twimlets.com/echo?Twiml="
ACCOUNT_SID = re.compile(r"AC[0-9a-f]{32}")
NUMBER_SID = re.compile(r"PN[0-9a-f]{32}")
TIMEOUT_S = 15.0
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
Out = Callable[[str], None]


class UsageError(ValueError):
    """Bad arguments or settings; the message never holds a secret."""


@dataclass(frozen=True)
class Plan:
    target: str  # https origin, no trailing slash
    numbers: dict[str, tuple[str, ...]]  # E.164 → the lanes it serves
    fallback_twiml: str
    fallback_url: str

    @property
    def voice_url(self) -> str:
        return f"{self.target}/twilio/voice"

    @property
    def status_url(self) -> str:
        return f"{self.target}/twilio/status"

    def fields(self) -> dict[str, str]:
        """The IncomingPhoneNumber update for every number."""
        return {"VoiceUrl": self.voice_url, "VoiceMethod": "POST", "StatusCallback": self.status_url,
                "StatusCallbackMethod": "POST", "VoiceFallbackUrl": self.fallback_url, "VoiceFallbackMethod": "GET"}


def https_url(value: str, what: str, *, origin: bool) -> str:
    """``value`` without a trailing slash when it is https (and, for an ``origin``, has no path, query or fragment)."""
    text = value.strip()
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        raise UsageError(f"{what} must be an https URL (got {text[:80]!r})")
    if origin and (parts.path not in ("", "/") or parts.query or parts.fragment):
        raise UsageError(f"{what} must be an origin such as https://restaurantos.fly.dev (no path or query)")
    return text.rstrip("/") if origin else text


def mask_sid(sid: str | None) -> str:
    return f"AC…{sid[-4:]}" if sid and ACCOUNT_SID.fullmatch(sid) else "(not set)"


def build_plan(settings: Settings, target: str | None, fallback_url: str | None = None) -> Plan:
    base = https_url(target or settings.public_base_url, "the target (--target or RESTOS_PUBLIC_BASE_URL)",
                     origin=True)
    lanes_of: dict[str, list[str]] = {}
    for lane, number in numbers.lane_numbers(settings).items():
        lanes_of.setdefault(number, []).append(lane)
    if not lanes_of:
        raise UsageError("no phone number is configured: set RESTOS_PHONE_NUMBERS, or TWILIO_JUDGE_NUMBER and "
                         "TWILIO_PUBLIC_NUMBER in twilio_credentials")
    twiml = f"<Response><Say>{escape(say_text('busy', base))}</Say><Hangup/></Response>"
    fallback = (https_url(fallback_url, "--fallback-url", origin=False) if fallback_url
                else ECHO_TWIMLET + quote(twiml, safe=""))
    return Plan(base, {number: tuple(lanes) for number, lanes in lanes_of.items()}, twiml, fallback)


def describe(plan: Plan, account_sid: str | None, *, apply: bool) -> list[str]:
    mode = "applying" if apply else "dry run: nothing is changed"
    lines = [f"Twilio setup for account {mask_sid(account_sid)} ({mode})",
             f"  voice URL   {plan.voice_url}  (POST)",
             f"  status      {plan.status_url}  (POST)",
             f"  fallback    {plan.fallback_url}  (GET)"]
    for number, lanes in plan.numbers.items():
        shared = "; one number serves both lanes, so calls start on the public lane" if len(lanes) > 1 else ""
        lines.append(f"  number {numbers.mask(number)}  lanes {' + '.join(lanes)}{shared}")
    lines += [f"The server's RESTOS_PUBLIC_BASE_URL must be {plan.target}, or Twilio's signatures will not verify.",
              "Fallback TwiML (put it in a TwiML Bin and pass the bin's URL as --fallback-url to use the bin):",
              f"  {plan.fallback_twiml}"]
    return lines


def _body(response: httpx.Response) -> dict[str, Any]:
    """The JSON object Twilio answered with; empty when the body is not one."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _twilio_error(response: httpx.Response) -> str:
    body = _body(response)
    return f" (Twilio {body.get('code', '?')}: {str(body.get('message', ''))[:160]})" if body else ""


def apply_plan(plan: Plan, client: httpx.Client, account_sid: str, out: Out) -> int:
    """Find each number on the account and update it; EXIT_FAIL when any number could not be set."""
    ok = True
    root = f"{API}/Accounts/{account_sid}/IncomingPhoneNumbers"
    for number in plan.numbers:
        shown = numbers.mask(number)
        found = client.get(f"{root}.json", params={"PhoneNumber": number})
        if found.status_code != 200:
            out(f"FAIL {shown}: the lookup returned HTTP {found.status_code}{_twilio_error(found)}")
            ok = False
            continue
        listed: list[Any] = _body(found).get("incoming_phone_numbers") or []
        current = next((n for n in listed if isinstance(n, dict) and numbers.e164(n.get("phone_number")) == number),
                       None)
        if current is None or not NUMBER_SID.fullmatch(str(current.get("sid", ""))):
            out(f"FAIL {shown}: not a number on this account")
            ok = False
            continue
        fields = plan.fields()
        for key, name in (("voice_application_sid", "VoiceApplicationSid"), ("trunk_sid", "TrunkSid")):
            if current.get(key):  # either one would override the voice URL
                fields[name] = ""
                out(f"note {shown}: clearing its {name}, which would override the voice URL")
        updated = client.post(f"{root}/{current['sid']}.json", data=fields)
        if updated.status_code != 200:
            out(f"FAIL {shown}: the update returned HTTP {updated.status_code}{_twilio_error(updated)}")
            ok = False
            continue
        body = _body(updated)
        got = (body.get("voice_url"), body.get("status_callback"), body.get("voice_fallback_url"))
        if got != (plan.voice_url, plan.status_url, plan.fallback_url):
            out(f"FAIL {shown}: Twilio answered with other URLs than requested")
            ok = False
            continue
        out(f"OK   {shown}: voice URL, status callback and fallback set")
    return EXIT_OK if ok else EXIT_FAIL


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Point the Twilio number(s) at a deployment (dry run by default).")
    parser.add_argument("--target", help="https origin of the deployment (default: RESTOS_PUBLIC_BASE_URL)")
    parser.add_argument("--fallback-url", help="https URL of a TwiML Bin holding the printed fallback TwiML")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="print the plan only (default)")
    mode.add_argument("--apply", dest="apply", action="store_true", help="update the numbers on Twilio")
    parser.set_defaults(apply=False)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None,
         transport: httpx.BaseTransport | None = None, out: Out = print) -> int:
    """``transport`` replaces the network (tests pass ``httpx.MockTransport``)."""
    args = parse_args(argv)
    settings = settings or Settings()
    sid_secret, token_secret = settings.twilio_account_sid, settings.twilio_auth_token
    sid = sid_secret.get_secret_value() if sid_secret is not None else None
    token = token_secret.get_secret_value() if token_secret is not None else None
    redactor = Redactor([value for value in (sid, token) if value])

    def say(line: str) -> None:
        out(redactor.redact(line))  # belt and braces: nothing printed may carry the SID or the token

    try:
        plan = build_plan(settings, args.target, args.fallback_url)
    except UsageError as exc:
        say(f"error: {exc}")
        return EXIT_USAGE
    for line in describe(plan, sid, apply=args.apply):
        say(line)
    if not args.apply:
        say("Dry run: nothing changed. Re-run with --apply to update Twilio.")
        return EXIT_OK
    if sid is None or not ACCOUNT_SID.fullmatch(sid) or not token:
        say("error: --apply needs TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN (environment or twilio_credentials)")
        return EXIT_USAGE
    with httpx.Client(auth=(sid, token), timeout=TIMEOUT_S, transport=transport) as http:
        try:
            return apply_plan(plan, http, sid, say)
        except httpx.HTTPError as exc:  # never the exception text: it can carry the request URL
            say(f"FAIL: {type(exc).__name__} while talking to Twilio")
            return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
