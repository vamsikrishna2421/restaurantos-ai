"""The image ships committed, allowlisted files only (ARCHITECTURE §3, ARCHITECTURE_OPS §13).

``.dockerignore`` is evaluated with a port of Docker's own matcher (moby/patternmatcher ``ReadAll`` and
``MatchesOrParentMatches``: the last matching line wins, ``**`` spans folders, a pattern also matches every parent
folder of a path) and of its context walk (an excluded folder is skipped unless an exception pattern starts inside it).
The walk reads names only, never file contents. The same module checks the Dockerfile's COPY sources, the Fly configs
and that prod is promoted from the image staging runs.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from restaurantos.config import ENV_CAPS

ROOT = Path(__file__).resolve().parents[2]
SECRET_NAMES = ("meta_muse_api_key", "twilio_credentials", "elevenlabs_api_key", "fly_token")
ALLOWED_PREFIXES = ("pyproject.toml", "requirements.lock", "src/restaurantos/", "src/web/", "config/",
                    "scripts/build_phrase_cache.py")


# ----- a port of Docker's .dockerignore matcher -------------------------------------------------------------------


@dataclass(frozen=True)
class Pattern:
    text: str  # cleaned, without the "!"
    exclusion: bool  # a "!" line re-includes
    kind: str  # exact | prefix | suffix | regexp
    regex: re.Pattern[str] | None


def read_patterns(text: str) -> list[Pattern]:
    """``ReadAll`` + ``NewPatternMatcher``: comments only at column 0, then trim, clean and drop a leading ``/``."""
    out = []
    for raw in text.splitlines():
        if raw.startswith("#") or not (line := raw.strip()):
            continue
        exclusion = line.startswith("!")
        line = posixpath.normpath(line[1:].strip() if exclusion else line)
        if len(line) > 1 and line.startswith("/"):
            line = line[1:]
        out.append(compile_pattern(line, exclusion))
    return out


def compile_pattern(text: str, exclusion: bool) -> Pattern:
    kind, regex, i, step = "exact", "^", 0, 0
    while i < len(text):
        ch = text[i]
        if ch == "*" and text[i + 1:i + 2] == "*":  # some flavour of **
            i += 2
            if text[i:i + 1] == "/":
                i += 1
            if i >= len(text):
                kind, regex = ("prefix", regex) if kind == "exact" else ("regexp", regex + ".*")
            else:
                kind, regex = "regexp", regex + "(.*/)?"
            if step == 0:
                kind = "suffix"
            step += 1
            continue
        if ch == "*":
            kind, regex = "regexp", regex + "[^/]*"
        elif ch == "?":
            kind, regex = "regexp", regex + "[^/]"
        elif ch in ".+()|{}$":
            regex += "\\" + ch
        elif ch == "\\":
            regex += "\\" + text[i + 1] if i + 1 < len(text) else "\\\\"
            i += 1
        elif ch in "[]":
            kind, regex = "regexp", regex + ch
        else:
            regex += ch
        i += 1
        step += 1
    return Pattern(text, exclusion, kind, re.compile(regex + "$") if kind == "regexp" else None)


def matches(p: Pattern, path: str) -> bool:
    if p.kind == "exact":
        return path == p.text
    if p.kind == "prefix":
        return path.startswith(p.text[:-2])
    if p.kind == "suffix":
        suffix = p.text[2:]
        return path.endswith(suffix) or (suffix.startswith("/") and path == suffix[1:])
    return p.regex is not None and p.regex.match(path) is not None


def excluded(patterns: list[Pattern], path: str) -> bool:
    """``MatchesOrParentMatches``: the path or any parent folder; later patterns override earlier ones."""
    matched = False
    parents = posixpath.dirname(path).split("/") if posixpath.dirname(path) else []
    for p in patterns:
        if p.exclusion != matched:
            continue
        if matches(p, path) or any(matches(p, "/".join(parents[:n])) for n in range(1, len(parents) + 1)):
            matched = not p.exclusion
    return matched


def context_files(root: Path, patterns: list[Pattern]) -> set[str]:
    """The files Docker would send: excluded folders are skipped unless an exception pattern starts inside them."""
    exceptions = [p.text + "/" for p in patterns if p.exclusion]
    files: set[str] = set()
    for folder, dirs, names in os.walk(root):
        rel = Path(folder).relative_to(root).as_posix()
        prefix = "" if rel == "." else rel + "/"
        dirs[:] = [d for d in dirs if not excluded(patterns, prefix + d)
                   or any(e.startswith(prefix + d + "/") for e in exceptions)]
        files.update(prefix + name for name in names if not excluded(patterns, prefix + name))
    return files


@pytest.fixture(scope="module")
def patterns() -> list[Pattern]:
    return read_patterns((ROOT / ".dockerignore").read_text(encoding="utf-8"))


def _is_secret(path: str) -> bool:
    name = posixpath.basename(path)
    return (name in SECRET_NAMES or name.endswith("_credentials") or name == ".env" or name.startswith(".env.")
            or name.endswith((".key", ".pem")))


# ----- .dockerignore ------------------------------------------------------------------------------------------------


def test_matcher_port_follows_docker_semantics() -> None:
    pats = read_patterns("*\n!src/app/**\nsrc/app/node_modules\n**/__pycache__\n**/.env.*\n# note\n!keep.txt\n")
    assert excluded(pats, "README.md") and excluded(pats, "src/other.py")
    assert not excluded(pats, "src/app/main.py") and not excluded(pats, "keep.txt")
    assert excluded(pats, "src/app/node_modules/x/index.js")  # a parent folder matches, and the last match wins
    assert excluded(pats, "src/app/pkg/__pycache__/m.pyc")
    assert excluded(pats, "src/app/.env.local") and not excluded(pats, "src/app/env.local")
    assert read_patterns("foo # not a comment")[0].text == "foo # not a comment"


def test_dockerignore_excludes_secrets(tmp_path: Path, patterns: list[Pattern]) -> None:
    secrets = [*SECRET_NAMES, ".env", ".env.local", "config/meta_muse_api_key", "config/twilio_credentials",
               "src/restaurantos/elevenlabs_api_key", "src/restaurantos/meta/fly_token", "src/web/.env",
               "src/web/.env.production", "config/tenants/acme_credentials", "src/restaurantos/server.key",
               "config/cert.pem"]
    private = ["data/ai_calls.jsonl", "data/caps.json", "data/ros.db", "data/recordings/call.wav",
               "data/eval_audio/fleurs/manifest.jsonl", "docs/planning/CONTRACTS.md", "docs/planning/BUILD_LOG.md",
               "tests/conftest.py", ".git/config", ".venv/bin/activate", ".claude/settings.json", "ruvector.db",
               "src/web/node_modules/react/index.js", "src/web/dist/index.html", "scripts/deploy.sh",
               "scripts/secrets_to_fly.sh", "src/restaurantos/__pycache__/app.cpython-312.pyc", "README.md"]
    needed = ["pyproject.toml", "requirements.lock", "src/restaurantos/__init__.py", "src/restaurantos/app.py",
              "src/restaurantos/voice/tts/base.py", "src/web/package.json", "src/web/package-lock.json",
              "src/web/index.html", "src/web/src/main.tsx", "config/features.yaml",
              "config/tenants/spice_route/menu.yaml", "scripts/build_phrase_cache.py"]
    for rel in secrets + private + needed:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")

    assert context_files(tmp_path, patterns) == set(needed)

    # The real tree (names only): no credential file, no data/, docs/planning/, tests or local tooling.
    real = context_files(ROOT, patterns)
    assert not [p for p in real if _is_secret(p)]
    assert not [p for p in real if p.startswith(("data/", "docs/", "tests/", ".git/", ".venv/", ".claude", ".swarm/"))]
    assert {"pyproject.toml", "src/restaurantos/app.py", "src/restaurantos/__main__.py", "config/features.yaml",
            "src/web/package.json", "src/web/package-lock.json", "src/web/index.html"} <= real


def test_real_context_is_the_allowlist(patterns: list[Pattern]) -> None:
    assert patterns[0].text == "*" and not patterns[0].exclusion  # exclude everything first
    real = context_files(ROOT, patterns)
    assert not sorted(p for p in real if not p.startswith(ALLOWED_PREFIXES))
    assert not [p for p in real if "/__pycache__/" in p or p.endswith(".pyc")]
    assert not [p for p in real if p.startswith(("src/web/node_modules/", "src/web/dist/"))]


# ----- Dockerfile -------------------------------------------------------------------------------------------------


def _copy_sources(dockerfile: str) -> list[str]:
    sources = []
    for line in dockerfile.replace("\\\n", " ").splitlines():
        words = line.split()
        if not words or words[0].upper() != "COPY" or any(w.startswith("--from=") for w in words):
            continue
        sources += [w for w in words[1:] if not w.startswith("--")][:-1]
    return sources


def test_dockerfile_copies_only_allowlisted_paths(patterns: list[Pattern]) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"^\s*ADD\s", dockerfile, re.MULTILINE | re.IGNORECASE)
    sources = _copy_sources(dockerfile)
    assert sources and "." not in sources and "./" not in sources
    real = context_files(ROOT, patterns)
    for src in sources:
        path = src.rstrip("*")
        if path.endswith("/"):
            assert any(f.startswith(path) for f in real), f"COPY {src}: nothing allowlisted under it"
        else:
            assert not excluded(patterns, path), f"COPY {src} is not allowlisted in .dockerignore"
        assert path.startswith(ALLOWED_PREFIXES), src


def test_dockerfile_runtime_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "node:22-slim" in dockerfile and "python:3.12-slim" in dockerfile
    for env in ("RESTOS_HOST=0.0.0.0", "RESTOS_DATA_DIR=/data", "RESTOS_FEATURES_PATH=/app/config/features.yaml"):
        assert env in dockerfile
    assert "ENV RESTOS_GIT_SHA=${RESTOS_GIT_SHA}" in dockerfile  # /api/health reports it
    assert "python -m restaurantos" in dockerfile and "RESTOS_MAINTENANCE" in dockerfile
    assert "setpriv --reuid=restos" in dockerfile  # the app never runs as root
    assert not re.search(r"^\s*USER\s+root", dockerfile, re.MULTILINE)


# ----- Fly configs ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("file", "app", "env"), [("fly.toml", "restaurantos", "prod"),
                                                  ("fly.staging.toml", "restaurantos-staging", "staging")])
def test_fly_config_always_on_liveness_only(file: str, app: str, env: str) -> None:
    cfg = tomllib.loads((ROOT / file).read_text(encoding="utf-8"))
    assert cfg["app"] == app and cfg["primary_region"] == "iad"
    service = cfg["http_service"]
    assert service["auto_stop_machines"] in ("off", False) and service["min_machines_running"] >= 1
    assert service["internal_port"] == 8000 and service["concurrency"]["type"] == "connections"
    assert [c["path"] for c in service["checks"]] == ["/api/health"]  # liveness only, never /api/health/deep
    assert cfg["mounts"] == [{"source": "ros_data", "destination": "/data", "initial_size": "3gb"}]
    e = cfg["env"]
    assert e["RESTOS_ENVIRONMENT"] == env and e["RESTOS_AI_MODE"] == "live" and e["RESTOS_DATA_DIR"] == "/data"
    assert (float(e["RESTOS_DAILY_USD_CAP"]), float(e["RESTOS_TOTAL_USD_CAP"])) == ENV_CAPS[env]  # explicit caps
    assert e["RESTOS_PUBLIC_BASE_URL"] == f"https://{app}.fly.dev" and e["RESTOS_BEHIND_PROXY"] == "fly"
    secretish = [k for k in e if re.search(r"KEY|TOKEN|SECRET|PASSCODE|SID|DTMF", k) and k != "RESTOS_KEY_FILES"]
    assert not secretish, "secrets belong in fly secrets (scripts/secrets_to_fly.sh), never in fly.toml"


# ----- same-image promotion ---------------------------------------------------------------------------------------


def _stub_fly(tmp_path: Path, image: str) -> Path:
    stub = tmp_path / "fly"
    machines = json.dumps([{"id": "m1", "state": "started", "config": {"image": image}}])
    stub.write_text("#!/bin/bash\n"
                    'if [ "$1 $2" = "machines list" ]; then echo \'' + machines + "'; exit 0; fi\n"
                    'echo "stub fly: $*" >&2\n', encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _deploy(tmp_path: Path, stub: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "FLY_BIN": str(stub),
           "TMPDIR": str(tmp_path)}
    return subprocess.run(["bash", str(ROOT / "scripts" / "deploy.sh"), *args], cwd=ROOT, env=env, text=True,
                          capture_output=True, timeout=60, check=False)


def test_deploy_promotes_the_image_staging_runs(tmp_path: Path) -> None:
    image = "registry.fly.io/restaurantos-staging:git-0123456789ab"
    done = _deploy(tmp_path, _stub_fly(tmp_path, image), "prod", "--dry-run", "--yes", "--hotfix", "--skip-smoke")
    assert done.returncode == 0, done.stderr
    deploys = [line for line in done.stderr.splitlines() if " deploy " in line]
    assert len(deploys) == 1 and f"--app restaurantos --image {image}" in deploys[0]  # the same image, no build
    assert "--remote-only" not in deploys[0] and "--build-arg" not in deploys[0]

    other = _deploy(tmp_path, _stub_fly(tmp_path, "registry.fly.io/restaurantos-staging:deployment-01ABC"),
                    "prod", "--dry-run", "--yes", "--hotfix")
    assert other.returncode != 0 and "not a scripts/deploy.sh staging build" in other.stderr


def test_deploy_staging_builds_a_clean_commit(tmp_path: Path) -> None:
    head = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, text=True, capture_output=True,
                          check=False)
    if head.returncode != 0:
        pytest.skip("not a git checkout")
    sha = head.stdout.strip()
    done = _deploy(tmp_path, _stub_fly(tmp_path, ""), "staging", "--dry-run", "--skip-smoke")
    assert done.returncode == 0, done.stderr
    (deploy,) = [line for line in done.stderr.splitlines() if " deploy " in line]
    assert "--app restaurantos-staging --remote-only" in deploy and "/tree --config " in deploy  # a clean checkout
    assert f"--image-label git-{sha}" in deploy and f"--build-arg RESTOS_GIT_SHA={sha}" in deploy
