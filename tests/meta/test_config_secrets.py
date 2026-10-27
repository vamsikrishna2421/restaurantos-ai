"""CP0 fixes on Settings: a blank secret counts as unset, a secret is one printable token (a CR or LF would
reach an HTTP header, whose error can quote it), the credential files are read in dev only and the ElevenLabs
file may carry the voice id, and money caps must be finite (NaN would switch every cap comparison off)."""

import logging

import pytest
from pydantic import SecretStr, ValidationError

from restaurantos.config import BootError, Settings

SECRETS = ("MODEL_API_KEY", "RESTOS_SESSION_SECRET", "RESTOS_JUDGE_PASSCODE", "RESTOS_OPS_TOKEN")


def prod_env(clean_env, tmp_path) -> None:
    """Everything the prod boot validator asks for, so a test can break one thing at a time."""
    (tmp_path / ".volume-id").write_text("vol-1")
    for name, value in (("RESTOS_DAILY_USD_CAP", "8"), ("RESTOS_TOTAL_USD_CAP", "85"), ("RESTOS_AI_MODE", "live"),
                        ("MODEL_API_KEY", "LLM_9_prod-secret"), ("RESTOS_SESSION_SECRET", "sess-secret"),
                        ("RESTOS_JUDGE_PASSCODE", "judge-secret"), ("RESTOS_OPS_TOKEN", "ops-secret"),
                        ("RESTOS_ADMIN_TOKENS", "vamsi:adm-secret")):
        clean_env.setenv(name, value)


def boot_problems(tmp_path, settings: Settings | None = None) -> list[str]:
    try:
        (settings or Settings(_env_file=None, environment="prod", data_dir=tmp_path)).validate_for_boot()
    except BootError as exc:
        return exc.problems
    return []


def test_blank_secrets_count_as_unset(clean_env, tmp_path):
    prod_env(clean_env, tmp_path)
    assert boot_problems(tmp_path) == []
    for blank in ("", "   ", "\r\n"):  # e.g. `fly secrets set X=$UNSET`, or a value that was only a line ending
        for name in SECRETS:
            clean_env.setenv(name, blank)
        problems = "\n".join(boot_problems(tmp_path))
        assert all(name in problems for name in SECRETS)
    s = Settings(_env_file=None, environment="prod", data_dir=tmp_path)
    assert s.model_api_key is s.session_secret is s.judge_passcode is s.ops_token is None
    with pytest.raises(RuntimeError):
        s.api_key()
    # A blank assigned after construction is still caught by the validator.
    prod_env(clean_env, tmp_path)
    late = Settings(_env_file=None, environment="prod", data_dir=tmp_path)
    late.judge_passcode = SecretStr(" ")
    assert boot_problems(tmp_path, late) == ["RESTOS_JUDGE_PASSCODE must be set"]
    for tokens in ("vamsi:", "vamsi: ,akshitha:tok"):  # an empty admin token never parses
        clean_env.setenv("RESTOS_ADMIN_TOKENS", tokens)
        with pytest.raises(ValidationError):
            Settings(_env_file=None, environment="prod", data_dir=tmp_path)
    # Dev makes its own session secret when the variable is blank.
    clean_env.delenv("RESTOS_ADMIN_TOKENS")
    clean_env.setenv("RESTOS_SESSION_SECRET", "")
    dev = Settings(_env_file=None, key_files=False, data_dir=tmp_path)
    assert dev.session_secret is not None and dev.session_secret.get_secret_value().strip()


def test_secrets_are_single_printable_tokens(clean_env, tmp_path):
    clean_env.setenv("MODEL_API_KEY", "LLM_1_fake-key\r\n")  # pasted from a CRLF file: stripped
    assert Settings(_env_file=None, key_files=False, data_dir=tmp_path).api_key() == "LLM_1_fake-key"
    ops = Settings(_env_file=None, key_files=False, data_dir=tmp_path, ops_token=SecretStr(" ops-1 ")).ops_token
    assert ops is not None and ops.get_secret_value() == "ops-1"
    for field in ("model_api_key", "judge_passcode", "elevenlabs_api_key", "twilio_auth_token", "session_secret"):
        for bad in ("tok\r\nX-Injected: 1", "two words", "tab\there", "nul\x00byte", "accént"):
            with pytest.raises(ValidationError) as exc:
                Settings(_env_file=None, key_files=False, data_dir=tmp_path, **{field: bad})
            assert bad not in str(exc.value) and field in str(exc.value)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, key_files=False, data_dir=tmp_path, admin_tokens="vamsi:two words")
    assert "two words" not in str(exc.value)


def test_key_files_dev_only_and_cleaned(clean_env, tmp_path, caplog):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "meta_muse_api_key").write_text("LLM_1_fake-meta\r\n")
    (keys / "twilio_credentials").write_text("TWILIO_ACCOUNT_SID=ACfake\r\nTWILIO_AUTH_TOKEN=tw fake\r\n")
    with caplog.at_level(logging.WARNING, logger="restaurantos.config"):
        dev = Settings(_env_file=None, key_files=True, key_files_dir=keys, data_dir=tmp_path)
    assert dev.api_key() == "LLM_1_fake-meta" and dev.twilio_account_sid.get_secret_value() == "ACfake"
    assert dev.twilio_auth_token is None  # a token with a space inside is refused, with a warning that never quotes it
    assert "ignoring twilio_auth_token" in caplog.text and "tw fake" not in caplog.text
    assert dev.secrets_from_files == {"model_api_key", "twilio_account_sid"}
    test = Settings(_env_file=None, key_files=True, key_files_dir=keys, data_dir=tmp_path, environment="test")
    assert test.model_api_key is None and test.secrets_from_files == frozenset()


def test_elevenlabs_file_key_and_voice_id(clean_env, tmp_path, caplog):
    keys = tmp_path / "keys"
    keys.mkdir()

    def load(text: str, **kw) -> Settings:
        (keys / "elevenlabs_api_key").write_text(text)
        return Settings(_env_file=None, key_files=True, key_files_dir=keys, data_dir=tmp_path, **kw)

    s = load("ELEVENLABS_API_KEY=sk_fake123\nTTS_VOICE_ID=voice_fake\n")
    assert s.elevenlabs_api_key.get_secret_value() == "sk_fake123" and s.tts_voice_id == "voice_fake"
    s = load("# ElevenLabs\nexport ELEVENLABS_API_KEY='sk_fake456'\n")
    assert s.elevenlabs_api_key.get_secret_value() == "sk_fake456" and s.tts_voice_id is None
    s = load("sk_fake789==\n")  # the raw key, even one ending in '=' padding
    assert s.elevenlabs_api_key.get_secret_value() == "sk_fake789==" and s.tts_voice_id is None
    s = load("ELEVENLABS_API_KEY=sk_fake1\nTTS_VOICE_ID=voice_file\n", tts_voice_id="voice_env")
    assert s.tts_voice_id == "voice_env"  # the environment wins
    with caplog.at_level(logging.WARNING, logger="restaurantos.config"):
        s = load("sk_fake_a\nsk_fake_b\n")  # two raw lines: never sent as one multi-line key
    assert s.elevenlabs_api_key is None and "elevenlabs_api_key" in caplog.text and "sk_fake_a" not in caplog.text


def test_money_caps_must_be_finite(clean_env, tmp_path):
    for field in ("daily_usd_cap", "total_usd_cap", "sandbox_usd_cap", "sandbox_usd_cap_passcode", "call_usd_cap",
                  "openweight_daily_usd_cap"):
        for bad in ("nan", "NaN", "inf", "-Infinity", -0.01, float("nan")):
            with pytest.raises(ValidationError):
                Settings(_env_file=None, key_files=False, data_dir=tmp_path, **{field: bad})
    clean_env.setenv("RESTOS_CALL_USD_CAP", "nan")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, key_files=False, data_dir=tmp_path)
    clean_env.delenv("RESTOS_CALL_USD_CAP")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, key_files=False, spend_pools={"build": {"public": {"daily": "nan", "total": 2}}})
    assert Settings(_env_file=None, key_files=False, data_dir=tmp_path, call_usd_cap=0).call_usd_cap == 0
