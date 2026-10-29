"""Pre-commit secret check: the files ``git add -A`` would stage (or, with ``--staged``, the files already staged)
searched for this laptop's real secret values and for key shapes. It prints names, never values.

    .venv/bin/python scripts/secret_scan.py            # what `git add -A` would stage
    .venv/bin/python scripts/secret_scan.py --staged   # what is staged now (stage explicit paths first)

The real values come from the gitignored key files at the repo root (``meta_muse_api_key``, ``twilio_credentials``,
``elevenlabs_api_key``); the shapes are a Meta key, a Twilio Account SID, an ElevenLabs key and a PEM private key.
Exit 1 on any hit. CI's gitleaks scan covers the shapes too; only this script knows the real values.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS: dict[str, re.Pattern[bytes]] = {
    "meta_key_shape": re.compile(rb"LLM_\d+_[A-Za-z0-9_-]{16,}"),
    "twilio_sid_shape": re.compile(rb"\bAC[0-9a-fA-F]{32}\b"),
    "eleven_key_shape": re.compile(rb"\bsk_[A-Za-z0-9]{24,}\b"),
    "private_key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def real_secrets(root: Path) -> dict[str, str]:
    """The secret values in this laptop's key files, by name (never printed)."""
    found: dict[str, str] = {}
    for name, file in (("META_KEY", "meta_muse_api_key"), ("ELEVENLABS_KEY", "elevenlabs_api_key")):
        path = root / file
        if path.is_file() and path.read_text().strip():
            found[name] = path.read_text().strip()
    twilio = root / "twilio_credentials"
    if twilio.is_file():
        for line in twilio.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") and value.strip():
                found[key.strip()] = value.strip()
    return found


def candidates(root: Path, *, staged: bool) -> list[str]:
    """The paths to scan, relative to ``root``."""
    if staged:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],  # noqa: S607
                             cwd=root, capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]
    out = subprocess.run(["git", "add", "-A", "--dry-run"], cwd=root,  # noqa: S607
                         capture_output=True, text=True, check=True).stdout
    return [line[5:-1] for line in out.splitlines() if line.startswith("add '")]


def main() -> int:
    staged = "--staged" in sys.argv[1:]
    secrets = real_secrets(ROOT)
    files = candidates(ROOT, staged=staged)
    hits = 0
    for rel in files:
        path = ROOT / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        for name, value in secrets.items():
            if value.encode() in data:
                print(f"SECRET VALUE {name} in {rel}")
                hits += 1
        for name, pattern in PATTERNS.items():
            if pattern.search(data):
                print(f"pattern {name} in {rel}")
                hits += 1
    print(f"scanned {len(files)} files, secrets loaded: {sorted(secrets)}, hits: {hits}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
