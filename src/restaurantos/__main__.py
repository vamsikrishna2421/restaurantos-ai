"""Run the server: ``python -m restaurantos``.

One uvicorn worker (the hub and voice sessions live in process). ``RESTOS_HOST`` (default 127.0.0.1) and
``RESTOS_PORT`` (default 8000) pick the socket. Proxy headers are on, so behind the Cloudflare tunnel on the founders'
laptop (cloudflared connects from 127.0.0.1) the app sees the public scheme and client address; on another host set
``FORWARDED_ALLOW_IPS`` to that proxy's address.
"""

from __future__ import annotations

import os

import uvicorn


def server_options() -> dict[str, object]:
    port = os.environ.get("RESTOS_PORT", "8000")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise SystemExit(f"RESTOS_PORT must be a TCP port number, got {port!r}")
    return {"host": os.environ.get("RESTOS_HOST", "127.0.0.1"), "port": int(port), "factory": True, "workers": 1,
            "proxy_headers": True, "forwarded_allow_ips": os.environ.get("FORWARDED_ALLOW_IPS"), "lifespan": "on"}


def main() -> None:
    uvicorn.run("restaurantos.app:create_app", **server_options())  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
