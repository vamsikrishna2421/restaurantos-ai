"""A minimal Chrome DevTools Protocol client for the demo-video scripts: launch a headless Chrome on its own profile,
drive one page (navigate, evaluate, click by text, wait for text) and take screenshots. No dependencies beyond
``websockets``, which the app already has."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import socket
import time
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import websockets

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Page:
    def __init__(self, ws: Any) -> None:
        self.ws, self.n = ws, 0

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str) -> Any:
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"page script failed: {r['exceptionDetails'].get('text')}")
        return r.get("result", {}).get("value")

    async def goto(self, url: str, settle_s: float = 3.0) -> None:
        await self.call("Page.navigate", url=url)
        await asyncio.sleep(settle_s)

    async def click(self, text: str, *, selector: str = "button, summary, a, [role=button]") -> bool:
        """Click the first visible element whose text contains ``text`` (scrolled to the middle first)."""
        found = await self.js(
            f"(() => {{ const el = [...document.querySelectorAll({json.dumps(selector)})]"
            f".find(e => e.offsetParent !== null && e.textContent.trim().includes({json.dumps(text)}));"
            " if (!el) return false; el.scrollIntoView({block: 'center'}); el.click(); return true })()")
        return bool(found)

    async def wait_text(self, text: str, timeout_s: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await self.js(f"document.body.innerText.includes({json.dumps(text)})"):
                return True
            await asyncio.sleep(0.5)
        return False

    async def scroll_to_text(self, text: str) -> bool:
        return bool(await self.js(
            "(() => { const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);"
            f" while (walker.nextNode()) {{ if (walker.currentNode.textContent.includes({json.dumps(text)})) {{"
            " walker.currentNode.parentElement.scrollIntoView({block: 'center'}); return true } } return false })()"))

    async def shot(self, path: Path, *, fmt: str = "png", quality: int = 90, fast: bool = False) -> Path:
        params: dict[str, Any] = {"format": fmt}
        if fmt == "jpeg":
            params["quality"] = quality
        if fast:  # skip the screenshot's own high-quality re-render: fine for video frames
            params["optimizeForSpeed"] = True
        r = await self.call("Page.captureScreenshot", **params)
        await asyncio.to_thread(_write, path, base64.b64decode(r["data"]))
        return path


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _page_target(port: int) -> dict[str, Any] | None:
    """The first page target of the Chrome on ``port``, or None while it is still starting."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:  # noqa: S310 - localhost
            return next((t for t in json.load(resp) if t["type"] == "page"), None)
    except OSError:
        return None


@contextlib.asynccontextmanager
async def chrome(profile: Path, *, width: int, height: int, scale: float = 1.0,
                 fake_media: bool = False) -> AsyncIterator[Page]:
    """A headless Chrome on ``profile`` with one page at ``width`` x ``height`` CSS pixels."""
    port = free_port()
    args = ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", "--mute-audio", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", f"--window-size={width},{height}", "about:blank"]
    if fake_media:
        args[:0] = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
    proc = await asyncio.create_subprocess_exec(CHROME, *args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        target = None
        for _ in range(100):
            target = await asyncio.to_thread(_page_target, port)
            if target is not None:
                break
            await asyncio.sleep(0.1)
        if target is None:
            raise RuntimeError("headless Chrome did not start")
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=200_000_000) as ws:
            page = Page(ws)
            await page.call("Page.enable")
            await page.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                            deviceScaleFactor=scale, mobile=False)
            yield page
    finally:
        proc.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 5)
