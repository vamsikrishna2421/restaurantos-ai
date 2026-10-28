"""P27a numbers and Twilio setup: E.164 parsing, withheld caller IDs, caller hashes, lanes by ``To`` (one number or
two), and ``scripts/twilio_setup.py`` (dry run by default, https only, secrets masked, ``--apply`` on a mock
transport; never the network)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote

import httpx
import pytest

from restaurantos.voice import numbers
from restaurantos.voice.numbers import WITHHELD_HASH, Caller
from tests.fakes.container import make_test_settings

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SECRET = "numbers-session-secret-0123456789"
SID = "AC" + "0123456789abcdef" * 2
AUTH = "a1b2c3d4" * 4
NUMBER = "+15087091967"
JUDGE_NO, PUBLIC_NO = "+15087091968", "+15087091969"
PN = {NUMBER: "PN" + "1" * 32, JUDGE_NO: "PN" + "2" * 32, PUBLIC_NO: "PN" + "3" * 32}
TARGET = "https://able-baker-1234.trycloudflare.com"


def _load(name: str) -> Any:
    module_name = f"restos_{name}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


setup = _load("twilio_setup")


def settings_for(**fields: Any) -> Any:
    base = {"session_secret": SECRET, "twilio_account_sid": SID, "twilio_auth_token": AUTH,
            "phone_numbers": {"public": NUMBER}}
    return make_test_settings(**(base | fields))


# ----- numbers ------------------------------------------------------------------------------------------------------


def test_e164_parsing():
    for raw in ("+15087091967", " +1 (508) 709-1967 ", "508.709.1967", "15087091967", "5087091967"):
        assert numbers.e164(raw) == NUMBER, raw
    assert numbers.e164("+442071838750") == "+442071838750"
    for raw in ("", "   ", "client:alice", "sip:alice@example.com", "+0123456789", "+123", "+1234567890123456",
                "abc", None, 15087091967):
        assert numbers.e164(raw) is None, raw


def test_withheld_spellings():
    for raw in ("+266696687", "+7378742833", "+86282452253", "+8656696", "+7748283", "+2562533", "anonymous",
                "Anonymous", "RESTRICTED", "unavailable", "", None, "client:alice"):
        assert numbers.is_withheld(raw), raw
    assert not numbers.is_withheld(NUMBER) and not numbers.is_withheld("+442071838750")


def test_caller_hash_and_identity():
    settings = settings_for()
    expected = hmac.new(SECRET.encode(), b"+15085550100", hashlib.sha256).hexdigest()
    assert numbers.caller_hash(settings, "+15085550100") == expected and len(expected) == 64
    same = numbers.identify(settings, "+1 508 555 0100")
    assert same == numbers.identify(settings, "+15085550100") == Caller(expected, "0100", withheld=False)
    assert "0100" in repr(same) and expected not in repr(same) and "5085550100" not in repr(same)
    assert numbers.identify(settings, "anonymous") == Caller(WITHHELD_HASH, None, withheld=True)
    assert numbers.caller_hash(settings_for(session_secret="another-secret-0123456789"), "+15085550100") != expected
    with pytest.raises(ValueError):
        numbers.caller_hash(settings, "5085550100")  # E.164 only: identify() normalizes first
    founders = settings_for(demo_caller_hashes=[" " + expected.upper(), "0" * 64])
    assert numbers.identify(founders, "+15085550100").founder
    assert not numbers.identify(founders, "+15085550101").founder
    assert not numbers.is_founder(settings, expected)


def test_lanes_by_to(caplog):
    one = settings_for(phone_numbers={"public": "(508) 709-1967"})  # one number: the settings copy it to both lanes
    assert numbers.lane_numbers(one) == {"judge": NUMBER, "public": NUMBER} and numbers.shared_number(one)
    assert numbers.lane_for(one, NUMBER) == "public"  # calls start on the public lane (D30)
    two = settings_for(phone_numbers={"judge": JUDGE_NO, "public": PUBLIC_NO})
    assert not numbers.shared_number(two)
    assert (numbers.lane_for(two, JUDGE_NO), numbers.lane_for(two, PUBLIC_NO)) == ("judge", "public")
    for to in ("+15550000000", "", None, "client:x"):
        assert numbers.lane_for(two, to) is None
    assert numbers.lane_for(settings_for(phone_numbers={}), NUMBER) is None  # nothing configured: no lane
    caplog.set_level(logging.WARNING)
    broken = settings_for(phone_numbers={"judge": "not-a-number", "public": PUBLIC_NO})
    assert numbers.lane_numbers(broken) == {"public": PUBLIC_NO}
    assert "phone_numbers.judge" in caplog.text and "not-a-number" not in caplog.text
    assert numbers.mask(NUMBER) == "…1967" and numbers.mask("nope") == "(no number)"


# ----- scripts/twilio_setup.py --------------------------------------------------------------------------------------


class FakeTwilio:
    """``IncomingPhoneNumbers`` list and update, recording every request."""

    def __init__(self, *, extra: dict[str, Any] | None = None, fail: int | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.extra = extra or {}
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            return httpx.Response(self.fail, json={"code": 20003, "message": f"Authenticate for /Accounts/{SID}"})
        if request.method == "GET":
            number = request.url.params["PhoneNumber"]
            listed = [{"sid": PN[number], "phone_number": number, **self.extra}] if number in PN else []
            return httpx.Response(200, json={"incoming_phone_numbers": listed})
        form = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
        return httpx.Response(200, json={"voice_url": form["VoiceUrl"], "status_callback": form["StatusCallback"],
                                         "voice_fallback_url": form["VoiceFallbackUrl"]})

    def updates(self) -> list[dict[str, str]]:
        return [dict(parse_qsl(r.content.decode(), keep_blank_values=True)) for r in self.requests
                if r.method == "POST"]


def run_setup(argv: list[str], settings: Any, twilio: FakeTwilio | None = None) -> tuple[int, str]:
    lines: list[str] = []
    transport = httpx.MockTransport(twilio) if twilio is not None else None
    code = setup.main(argv, settings=settings, transport=transport, out=lines.append)
    return code, "\n".join(lines)


def test_setup_dry_run_masks_secrets_and_makes_no_call():
    twilio = FakeTwilio()
    code, out = run_setup(["--target", TARGET + "/"], settings_for(), twilio)
    assert code == 0 and twilio.requests == []  # the default is a dry run: no network
    assert f"voice URL   {TARGET}/twilio/voice  (POST)" in out and f"status      {TARGET}/twilio/status  (POST)" in out
    assert "AC…cdef" in out and "…1967" in out and "lanes judge + public; one number serves both lanes" in out
    for secret in (SID, AUTH, NUMBER, "5087091967"):
        assert secret not in out
    fallback = setup.build_plan(settings_for(), TARGET)
    assert fallback.fallback_url.startswith(setup.ECHO_TWIMLET)
    assert unquote(fallback.fallback_url.removeprefix(setup.ECHO_TWIMLET)) == fallback.fallback_twiml
    assert fallback.fallback_twiml == ("<Response><Say>All our demo lines are busy. Please try the web demo at "
                                       "able-baker-1234.trycloudflare.com. Goodbye!</Say><Hangup/></Response>")
    assert fallback.fallback_twiml in out and "Dry run: nothing changed" in out


def test_setup_refuses_a_non_https_target():
    for argv in (["--target", "http://able-baker-1234.trycloudflare.com"], ["--target", TARGET + "/x"],
                 ["--target", TARGET + "?a=1"], [], ["--target", TARGET, "--fallback-url", "http://bin.example"]):
        twilio = FakeTwilio()
        code, out = run_setup([*argv, "--apply"], settings_for(), twilio)  # make_test_settings: http://127.0.0.1
        assert code == 2 and out.startswith("error:") and twilio.requests == [], argv
    code, out = run_setup(["--target", TARGET], settings_for(phone_numbers={}))
    assert code == 2 and "no phone number is configured" in out


def test_setup_apply_updates_each_number_once():
    twilio = FakeTwilio()
    code, out = run_setup(["--target", TARGET, "--apply"], settings_for(), twilio)
    assert code == 0 and "OK   …1967" in out
    assert [(r.method, r.url.path) for r in twilio.requests] == [  # one number serving both lanes: updated once
        ("GET", f"/2010-04-01/Accounts/{SID}/IncomingPhoneNumbers.json"),
        ("POST", f"/2010-04-01/Accounts/{SID}/IncomingPhoneNumbers/{PN[NUMBER]}.json")]
    assert twilio.requests[0].url.params["PhoneNumber"] == NUMBER
    basic = "Basic " + base64.b64encode(f"{SID}:{AUTH}".encode()).decode()
    assert all(r.headers["authorization"] == basic for r in twilio.requests)
    (fields,) = twilio.updates()
    plan = setup.build_plan(settings_for(), TARGET)
    assert fields == {"VoiceUrl": f"{TARGET}/twilio/voice", "VoiceMethod": "POST",
                      "StatusCallback": f"{TARGET}/twilio/status", "StatusCallbackMethod": "POST",
                      "VoiceFallbackUrl": plan.fallback_url, "VoiceFallbackMethod": "GET"}
    assert SID not in out and AUTH not in out

    both = FakeTwilio(extra={"voice_application_sid": "AP" + "9" * 32, "trunk_sid": None})
    bin_url = "https://handler.twilio.com/twiml/EH" + "4" * 32
    code, out = run_setup(["--target", TARGET, "--apply", "--fallback-url", bin_url],
                          settings_for(phone_numbers={"judge": JUDGE_NO, "public": PUBLIC_NO}), both)
    assert code == 0 and out.count("OK   ") == 2 and "clearing its VoiceApplicationSid" in out
    assert [u["VoiceFallbackUrl"] for u in both.updates()] == [bin_url, bin_url]
    assert all(u["VoiceApplicationSid"] == "" and "TrunkSid" not in u for u in both.updates())


def test_setup_apply_reports_twilio_errors_without_secrets():
    code, out = run_setup(["--target", TARGET, "--apply"], settings_for(), FakeTwilio(fail=401))
    assert code == 1 and "FAIL …1967: the lookup returned HTTP 401 (Twilio 20003" in out
    assert SID not in out and AUTH not in out
    code, out = run_setup(["--target", TARGET, "--apply"], settings_for(phone_numbers={"public": "+15550001111"}),
                          FakeTwilio())
    assert code == 1 and "not a number on this account" in out

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    lines: list[str] = []
    assert setup.main(["--target", TARGET, "--apply"], settings=settings_for(), transport=httpx.MockTransport(offline),
                      out=lines.append) == 1
    assert lines[-1] == "FAIL: ConnectError while talking to Twilio" and SID not in "\n".join(lines)
    for missing in ({"twilio_account_sid": None}, {"twilio_auth_token": None}, {"twilio_account_sid": "AC123"}):
        twilio = FakeTwilio()
        code, out = run_setup(["--target", TARGET, "--apply"], settings_for(**missing), twilio)
        assert code == 2 and "--apply needs TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN" in out and not twilio.requests
