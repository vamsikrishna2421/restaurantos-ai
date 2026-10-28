"""Secret handling in the ops scripts (ARCHITECTURE §3 "Secrets", ARCHITECTURE_OPS §13).

scripts/secrets_to_fly.sh pipes vendor and generated secrets into ``fly secrets import`` (a stub here) without printing
a value, even under ``bash -x``; keeps generated values outside the repo and reuses them; and produces values that pass
config.py's staging boot validator. dev.sh, smoke_deploy.py, the workflows and the gitleaks rules hold the same line.
Every fake key is assembled at runtime, so no key-shaped literal sits in this file for gitleaks to flag.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from restaurantos.app import create_app
from restaurantos.config import Settings
from tests.fakes.container import make_test_settings

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SECRETS_SH = SCRIPTS / "secrets_to_fly.sh"
GENERATED = ("RESTOS_SESSION_SECRET", "RESTOS_ADMIN_TOKENS", "RESTOS_JUDGE_PASSCODE", "RESTOS_OPS_TOKEN",
             "RESTOS_DEMO_DTMF_CODE")
FAKE_META = "LLM_" + "4242" + "_" + "Zq7" * 8
FAKE_SID = "AC" + "0f" * 16
FAKE_TWILIO_TOKEN = "9e" * 16
FAKE_ELEVEN = "sk_" + "Xy9" * 8
NUMBER = "+15087091967"


def _load(name: str) -> Any:
    module_name = f"restos_{name}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    return sys.modules[module_name]


smoke = _load("smoke_deploy")


def _base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), **extra}


def _pairs(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


def _leaked(value: str, output: str) -> bool:
    return bool(re.search(rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])", output)) if len(value) < 12 \
        else value in output


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "meta_muse_api_key").write_text(FAKE_META + "\n", encoding="utf-8")
    (keys / "twilio_credentials").write_text(
        f'TWILIO_ACCOUNT_SID={FAKE_SID}\nexport TWILIO_AUTH_TOKEN="{FAKE_TWILIO_TOKEN}"\n# one number, both lanes\n'
        f"TWILIO_PUBLIC_NUMBER={NUMBER}\n", encoding="utf-8")
    (keys / "elevenlabs_api_key").write_text(f"ELEVENLABS_API_KEY={FAKE_ELEVEN}\nTTS_VOICE_ID=voice42\n", "utf-8")
    stub = tmp_path / "fly"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$STUB_LOG.argv"\ncat >> "$STUB_LOG.stdin"\n'
                    'echo "stub fly: secrets staged"\n', encoding="utf-8")
    stub.chmod(0o755)
    return {"root": tmp_path, "keys": keys, "store": tmp_path / "store", "stub": stub, "log": tmp_path / "flylog"}


def _secrets(sb: dict[str, Path], *args: str, xtrace: bool = False) -> subprocess.CompletedProcess[str]:
    env = _base_env(sb["root"], FLY_BIN=str(sb["stub"]), STUB_LOG=str(sb["log"]), RESTOS_KEY_FILES_DIR=str(sb["keys"]),
                    RESTOS_SECRETS_DIR=str(sb["store"]))
    command = ["bash", *(["-x"] if xtrace else []), str(SECRETS_SH), *args]
    return subprocess.run(command, env=env, text=True, capture_output=True, timeout=60, check=False)


def _sent(sb: dict[str, Path]) -> dict[str, str]:
    return _pairs(sb["log"].with_suffix(".stdin").read_text(encoding="utf-8"))


# ----- secrets_to_fly.sh ------------------------------------------------------------------------------------------


def test_secrets_script_bash_syntax() -> None:
    assert subprocess.run(["bash", "-n", str(SECRETS_SH)], capture_output=True, check=False).returncode == 0


def test_secrets_script_static_no_echo() -> None:
    text = SECRETS_SH.read_text(encoding="utf-8")
    commands = [line for line in text.splitlines()[1:] if line.strip() and not line.lstrip().startswith("#")]
    assert commands[0].startswith("set +x")  # the first command: no trace even under `bash -x`
    assert not re.search(r"set\s+-[a-wyz]*x|set\s+-o\s+xtrace", text)
    assert "secrets set" not in text  # values never travel as argv (visible in ps)
    assert re.search(r'^emit \| "\$FLY" "\$\{args\[@\]\}"$', text, re.MULTILINE)  # only through a pipe


def test_secrets_script_never_echoes_values(sandbox: dict[str, Path]) -> None:
    done = _secrets(sandbox, "staging", xtrace=True)
    assert done.returncode == 0, done.stderr
    sent = _sent(sandbox)
    assert sent["MODEL_API_KEY"] == FAKE_META
    assert (sent["RESTOS_TWILIO_ACCOUNT_SID"], sent["RESTOS_TWILIO_AUTH_TOKEN"]) == (FAKE_SID, FAKE_TWILIO_TOKEN)
    assert json.loads(sent["RESTOS_PHONE_NUMBERS"]) == {"judge": NUMBER, "public": NUMBER}
    assert (sent["RESTOS_ELEVENLABS_API_KEY"], sent["RESTOS_TTS_VOICE_ID"]) == (FAKE_ELEVEN, "voice42")
    assert re.fullmatch(r"vamsi:[0-9a-f]{48},akshitha:[0-9a-f]{48}", sent["RESTOS_ADMIN_TOKENS"])
    assert re.fullmatch(r"[0-9a-f]{64}", sent["RESTOS_SESSION_SECRET"]) and re.fullmatch(r"\d{6}",
                                                                                        sent["RESTOS_DEMO_DTMF_CODE"])
    output = done.stdout + done.stderr
    assert not [name for name, value in sent.items() if _leaked(value, output)]
    argv = sandbox["log"].with_suffix(".argv").read_text(encoding="utf-8")
    assert argv.split() == ["secrets", "import", "--app", "restaurantos-staging", "--stage"]
    store = sandbox["store"] / "restaurantos-staging.env"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600 and stat.S_IMODE(store.parent.stat().st_mode) == 0o700
    assert {k: _pairs(store.read_text(encoding="utf-8"))[k] for k in GENERATED} == {k: sent[k] for k in GENERATED}


def test_secrets_script_reuses_and_rotates(sandbox: dict[str, Path]) -> None:
    assert _secrets(sandbox, "prod").returncode == 0
    first = _sent(sandbox)
    assert _secrets(sandbox, "prod").returncode == 0
    assert {k: _sent(sandbox)[k] for k in GENERATED} == {k: first[k] for k in GENERATED}  # a re-run rotates nothing
    assert _secrets(sandbox, "prod", "--rotate", "RESTOS_OPS_TOKEN").returncode == 0
    third = _sent(sandbox)
    assert third["RESTOS_OPS_TOKEN"] != first["RESTOS_OPS_TOKEN"]
    assert all(third[k] == first[k] for k in GENERATED if k != "RESTOS_OPS_TOKEN")
    dry = _secrets(sandbox, "prod", "--dry-run", "--without", "twilio")
    assert dry.returncode == 0 and "MODEL_API_KEY" in dry.stdout and "RESTOS_TWILIO_AUTH_TOKEN" not in dry.stdout
    assert FAKE_META not in dry.stdout + dry.stderr


def test_secrets_script_refuses_a_malformed_value_by_name(sandbox: dict[str, Path]) -> None:
    bad = "LLM_" + "77" + "_abc def"
    (sandbox["keys"] / "meta_muse_api_key").write_text(bad, encoding="utf-8")
    done = _secrets(sandbox, "staging")
    assert done.returncode != 0 and "MODEL_API_KEY" in done.stderr and bad not in done.stdout + done.stderr
    assert not sandbox["log"].with_suffix(".stdin").exists()  # nothing was sent


def test_secrets_script_values_pass_the_staging_boot_validator(sandbox: dict[str, Path], tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    assert _secrets(sandbox, "staging").returncode == 0
    data = tmp_path / "data"
    data.mkdir()
    (data / ".volume-id").write_text("volume=vol_test\n", encoding="utf-8")
    fly_toml = tomllib.loads((ROOT / "fly.staging.toml").read_text(encoding="utf-8"))["env"]
    for name, value in {**fly_toml, **_sent(sandbox), "RESTOS_DATA_DIR": str(data), "RESTOS_FAKE_ASR": "0"}.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings.validate_for_boot()  # BootError on any missing or malformed secret
    assert sorted(settings.admin_tokens) == ["akshitha", "vamsi"]
    assert settings.phone_numbers == {"judge": NUMBER, "public": NUMBER}
    assert settings.api_key() == FAKE_META and settings.demo_dtmf_code is not None


@pytest.mark.parametrize("script", ["secrets_to_fly.sh", "deploy.sh", "dev.sh", "backup_offhost.sh", "restore.sh"])
def test_ops_scripts_parse_and_print_help(script: str, tmp_path: Path) -> None:
    path = SCRIPTS / script
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True, check=False).returncode == 0
    done = subprocess.run(["bash", str(path), "--help"], env=_base_env(tmp_path), text=True, capture_output=True,
                          timeout=30, check=False)
    assert done.returncode == 0 and done.stdout.strip(), done.stderr


def test_backup_refuses_a_destination_inside_the_repo(tmp_path: Path) -> None:
    done = subprocess.run(["bash", str(SCRIPTS / "backup_offhost.sh"), "staging", "--dry-run", "--dest",
                           str(ROOT / "data" / "offhost")], env=_base_env(tmp_path, FLY_BIN="/usr/bin/true"),
                          text=True, capture_output=True, timeout=30, check=False)
    assert done.returncode != 0 and "outside the repo" in done.stderr
    assert not (ROOT / "data" / "offhost").exists()


# ----- dev.sh -----------------------------------------------------------------------------------------------------


def _free_port() -> str:
    """A port nothing on this machine listens on now (dev.sh refuses one in use; a fixed one collides)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def test_dev_sh_starts_the_tunnel_first_and_prints_no_secrets(tmp_path: Path) -> None:
    url = "https://smoke-test-words.trycloudflare.com"
    cloudflared = tmp_path / "cloudflared"
    cloudflared.write_text("#!/bin/bash\necho 'INF Requesting new quick Tunnel on trycloudflare.com...' >&2\n"
                           f"echo 'INF |  {url}  |' >&2\nwhile :; do sleep 1; done\n", encoding="utf-8")
    app = tmp_path / "py"  # the app records its environment; python -c (the setsid wrapper) runs for real
    app.write_text('#!/bin/bash\nif [ "$1" = "-m" ]; then env | grep -E "^RESTOS_(PUBLIC_BASE_URL|BEHIND_PROXY|HOST|'
                   f'PORT)=" | sort > "$APP_ENV_OUT"; exit 0; fi\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    for stub in (cloudflared, app):
        stub.chmod(0o755)
    admin = secrets.token_hex(24)
    env = _base_env(tmp_path, RESTOS_DEV_PYTHON=str(app), CLOUDFLARED_BIN=str(cloudflared), MODEL_API_KEY=FAKE_META,
                    RESTOS_DEV_STATE_DIR=str(tmp_path / "state"), APP_ENV_OUT=str(tmp_path / "app.env"),
                    RESTOS_ADMIN_TOKENS=f"vamsi:{admin}")

    def dev(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPTS / "dev.sh"), *args], env=env, text=True, capture_output=True,
                              timeout=90, check=False)

    pid, port = None, _free_port()
    try:
        first = dev("--tunnel", "--no-build", "--port", port)
        assert first.returncode == 0, first.stderr
        assert _pairs((tmp_path / "app.env").read_text(encoding="utf-8")) == {
            "RESTOS_BEHIND_PROXY": "cloudflare", "RESTOS_HOST": "127.0.0.1", "RESTOS_PORT": port,
            "RESTOS_PUBLIC_BASE_URL": url}
        assert url in first.stdout
        pid = int((tmp_path / "state" / "tunnel.pid").read_text(encoding="utf-8"))
        again = dev("--tunnel", "--no-build", "--port", port)
        assert again.returncode == 0 and "reusing the running tunnel" in again.stdout
        assert int((tmp_path / "state" / "tunnel.pid").read_text(encoding="utf-8")) == pid
        for result in (first, again):
            assert FAKE_META not in result.stdout + result.stderr and admin not in result.stdout + result.stderr
        assert dev("--stop-tunnel").returncode == 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and subprocess.run(["kill", "-0", str(pid)], capture_output=True,
                                                             check=False).returncode == 0:
            time.sleep(0.1)
        assert not (tmp_path / "state" / "tunnel.pid").exists()
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


# ----- smoke_deploy.py --------------------------------------------------------------------------------------------


def _stub_app(*, admin: str, ops: str, csp_host: str, sha: str) -> FastAPI:
    """A careless server that echoes credentials in its errors: the smoke output must still hold none."""
    app = FastAPI()
    csp = f"default-src 'self'; connect-src 'self' wss://{csp_host}; frame-ancestors 'none'"

    @app.get("/api/health")
    async def health() -> JSONResponse:
        body = {"status": "ok", "ai_mode": "live", "phase": "build", "version": "0.1.0", "git_sha": sha}
        return JSONResponse(body, headers={"content-security-policy": csp})

    @app.post("/api/admin/login")
    async def login(request: Request) -> Response:
        token = (await request.json()).get("token")
        if token != admin:
            return JSONResponse({"error": {"code": "forbidden_role", "message": f"bad {token}"}}, status_code=403)
        response = Response(status_code=204)
        response.set_cookie("ros_admin", "cookie" + "cd" * 20, httponly=True)
        return response

    @app.get("/api/health/deep")
    async def deep(request: Request) -> JSONResponse:
        if request.headers.get("authorization") != f"Bearer {ops}" or "ros_admin" not in request.cookies:
            return JSONResponse({"error": {"code": "no_session", "message": str(request.headers)}}, status_code=401)
        return JSONResponse({"db_ok": True, "meta_status": "ok", "last_canary": {"ok": None}})

    return app


async def _smoke(app: Any, base: str, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    lines: list[str] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as client:
        code = await smoke.run(smoke.parse_args(argv), env, http=client, out=lines.append)
    return code, "\n".join(lines)


async def test_smoke_deploy_never_prints_tokens(tmp_path: Path) -> None:
    admin, other, ops = secrets.token_hex(24), secrets.token_hex(24), secrets.token_hex(32)
    store = tmp_path / "store"
    store.mkdir()
    (store / "restaurantos-staging.env").write_text(
        f"RESTOS_ADMIN_TOKENS=vamsi:{admin},akshitha:{other}\nRESTOS_OPS_TOKEN={ops}\n", encoding="utf-8")
    env = {"RESTOS_SECRETS_DIR": str(store)}
    base = "https://restaurantos-staging.fly.dev"
    app = _stub_app(admin=admin, ops=ops, csp_host="restaurantos-staging.fly.dev", sha="0123456789ab")
    code, out = await _smoke(app, base, ["--target", "staging", "--health-only", "--expect-sha", "0123456"], env)
    assert code == 0, out
    assert re.search(r"^PASS health", out, re.M) and re.search(r"^PASS admin", out, re.M)
    assert re.search(r"^PASS deep", out, re.M)
    assert admin not in out and ops not in out and "cd" * 20 not in out

    wrong = secrets.token_hex(24)
    bad = _stub_app(admin=admin, ops=ops, csp_host="elsewhere.example", sha="0123456789ab")
    code, out = await _smoke(bad, base, ["--target", "staging", "--health-only", "--expect-sha", "fffffff"],
                             {**env, "RESTOS_SMOKE_ADMIN_TOKEN": wrong})
    assert code == 1 and "lacks wss://restaurantos-staging.fly.dev" in out and "git_sha '0123456789ab' is not" in out
    assert re.search(r"^FAIL admin .*HTTP 403 forbidden_role$", out, re.M) and wrong not in out and ops not in out


async def test_smoke_deploy_health_passes_against_the_app() -> None:
    app = create_app(make_test_settings())  # the real factory: /api/health, the CSP header, the SPA fallback
    code, out = await _smoke(app, "http://127.0.0.1:8000", ["--target", "local", "--health-only"], {})
    assert code == 0, out
    assert re.search(r"^PASS health +ai_mode=mock", out, re.M) and re.search(r"^SKIP deep", out, re.M)


def test_smoke_deploy_token_sources(tmp_path: Path) -> None:
    (tmp_path / "restaurantos.env").write_text("RESTOS_ADMIN_TOKENS=vamsi:a1,akshitha:b2\nRESTOS_OPS_TOKEN=o3\n",
                                               encoding="utf-8")
    env = {"RESTOS_SECRETS_DIR": str(tmp_path)}
    assert smoke.load_tokens("restaurantos", env) == smoke.Tokens(admin="a1", ops="o3")
    assert smoke.load_tokens("restaurantos", env, "akshitha").admin == "b2"
    assert smoke.load_tokens("restaurantos", {**env, "RESTOS_SMOKE_ADMIN_TOKEN": "z9"}).admin == "z9"
    assert smoke.load_tokens("restaurantos", {**env, "RESTOS_ADMIN_TOKENS": "lead:dev"}).admin == "a1"  # file wins
    assert smoke.load_tokens(None, {**env, "RESTOS_ADMIN_TOKENS": "lead:dev"}) == smoke.Tokens(admin="dev")


# ----- workflows, gitleaks and pre-commit -------------------------------------------------------------------------


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on:` key as True
    return on if isinstance(on, dict) else {name: None for name in ([on] if isinstance(on, str) else on)}


def test_workflows_hold_no_vendor_secrets() -> None:
    texts = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / ".github" / "workflows").glob("*.yml")}
    assert set(texts) == {"ci.yml", "ops.yml", "deploy.yml"}
    allowed = {"GITHUB_TOKEN", "FLY_API_TOKEN", "RESTOS_OPS_TOKEN"}  # no Meta, Twilio or ElevenLabs secret, ever
    for text in texts.values():
        assert "pull_request_target" not in _triggers(yaml.safe_load(text))
        assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)) <= allowed
        assert not re.search(r"MODEL_API_KEY|TWILIO_|ELEVENLABS_|meta_muse_api_key|twilio_credentials", text)
    deploy = yaml.safe_load(texts["deploy.yml"])
    assert set(_triggers(deploy)) == {"workflow_dispatch"}
    assert all("refs/heads/main" in job["if"] for job in deploy["jobs"].values())
    ci = yaml.safe_load(texts["ci.yml"])
    assert _triggers(ci)["push"]["branches"] == ["main", "wip/*"]
    steps = [step.get("name", step.get("uses", "")) for step in ci["jobs"]["test"]["steps"]]
    assert steps.index("Web build (tsc + vite), before pytest") < steps.index("Pytest (mock and replay only)")
    assert any(step.get("with", {}).get("path") == "src/web/node_modules" for step in ci["jobs"]["test"]["steps"])
    assert any("gitleaks" in step.get("uses", "") for step in ci["jobs"]["gitleaks"]["steps"])
    ops = yaml.safe_load(texts["ops.yml"])
    assert "schedule" in _triggers(ops) and ops["permissions"]["issues"] == "write"


def test_gitleaks_rules_catch_our_key_shapes() -> None:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert config["extend"]["useDefault"] is True
    rules = {rule["id"]: re.compile(rule["regex"]) for rule in config["rules"]}
    assert rules["meta-model-api-key"].pattern == "LLM_[0-9]+_[A-Za-z0-9_-]{16,}"  # ARCHITECTURE_OPS §13
    assert rules["meta-model-api-key"].search(f"key = '{FAKE_META}'")
    assert rules["elevenlabs-api-key"].search(f'key = "{FAKE_ELEVEN}"')
    assert not rules["elevenlabs-api-key"].search("tsk_" + "01JC" + "0" * 22)  # staff task ids are not keys
    assert rules["twilio-account-sid"].search(FAKE_SID)
    hooks = {hook["id"]: hook for repo in yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text("utf-8"))["repos"]
             for hook in repo["hooks"]}
    assert {"gitleaks", "ruff", "no-credential-files"} <= set(hooks)
    hook = hooks["no-credential-files"]

    def blocked(name: str) -> bool:
        return bool(re.search(hook["files"], name)) and not re.search(hook["exclude"], name)

    for name in ("meta_muse_api_key", "twilio_credentials", "elevenlabs_api_key", "fly_token", ".env", ".env.local",
                 "config/acme_credentials"):
        assert blocked(name), name
    assert not blocked("src/restaurantos/config.py") and not blocked(".env.example")
