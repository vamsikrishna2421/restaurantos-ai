"""Screenshots of the real app for the progress videos: a headless Chrome on a fresh profile (a new visitor, so a new
sample kitchen) walks a scenario on the running dev server and saves numbered PNGs.

    .venv/bin/python scripts/demo_video/capture.py https://<tunnel>.trycloudflare.com story
    .venv/bin/python scripts/demo_video/capture.py https://<tunnel>.trycloudflare.com kiosk

``story`` runs "Run the full story live" (real Meta models: about $0.02 to $0.03 of credit) and approves the plan's
steps in the sample kitchen; ``kiosk`` places a typed order through the fast path (no model call). Screens go to
``data/video_work/screens/<scenario>/`` (gitignored); the kitchen is reset at the end so it stops spending.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import Page, chrome  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
WORK = REPO / "data" / "video_work"
W, H = 1440, 900


async def story(p: Page, base: str, out: Path) -> None:
    await p.goto(base + "/", 4)
    await p.shot(out / "01-home.png")
    await p.click("Run the full story live")
    await asyncio.sleep(6)
    await p.scroll_to_text("The full story, live")
    await p.shot(out / "02-rush.png")
    await p.wait_text("Order #", 90)
    await asyncio.sleep(2)
    await p.scroll_to_text("Your order")
    await p.shot(out / "03-order.png")
    await p.wait_text("AI plan:", 150)
    await asyncio.sleep(2)
    await p.scroll_to_text("Playbook, then the AI plan")
    await p.shot(out / "04-plan.png")
    await p.wait_text("Approve", 60)
    await asyncio.sleep(1)
    await p.scroll_to_text("Your approval")
    await p.shot(out / "05-approval.png")
    await p.goto(base + "/gm", 5)
    await p.shot(out / "06-gm.png")
    await p.scroll_to_text("AI GM")
    await asyncio.sleep(1)
    await p.shot(out / "07-gm-plan.png")
    await p.goto(base + "/kds", 4)
    await p.shot(out / "08-kds.png")
    await p.goto(base + "/", 4)
    await p.scroll_to_text("Your approval")
    await p.click("Approve")
    await p.wait_text("steps verified", 90)
    await asyncio.sleep(2)
    await p.scroll_to_text("steps verified")
    await p.shot(out / "09-verified.png")
    await p.click("Verify the chain")
    await p.wait_text("Chain verified", 30)
    await asyncio.sleep(1)
    await p.shot(out / "10-chain.png")
    await p.goto(base + "/supplier", 4)
    await p.shot(out / "11-supplier.png")
    await p.goto(base + "/gm", 5)
    await p.scroll_to_text("Ledger")
    await p.shot(out / "12-gm-ledger.png")


async def kiosk(p: Page, base: str, out: Path) -> None:
    await p.goto(base + "/kiosk", 4)
    await p.shot(out / "01-gate.png")
    await p.click("Tap to start")
    await asyncio.sleep(7)
    await p.shot(out / "02-kiosk.png")
    await p.click("Type instead")  # with a microphone the typed box hides behind this toggle
    await asyncio.sleep(1)
    await p.js("(() => { const i = document.querySelector('input[placeholder^=\"e.g.\"]'); i.focus(); return true })()")
    await p.call("Input.insertText", text="two samosa and one mango lassi, that is all")
    await p.shot(out / "03-typed.png")
    await p.js("(() => { document.querySelector('button[aria-label=\"Send\"]').click(); return true })()")
    await p.wait_text("Say yes to place the order", 30)
    await asyncio.sleep(1)
    await p.shot(out / "04-readback.png")
    await p.click("Yes, place order")
    await asyncio.sleep(4)
    await p.shot(out / "05-placed.png")
    await p.click("తెలుగు")
    await asyncio.sleep(2)
    await p.shot(out / "06-telugu.png")


async def proof(p: Page, base: str, out: Path) -> None:
    """The /proof, /status and /privacy pages, with a sample kitchen whose rush has just started (no model call)."""
    await p.goto(base + "/", 4)
    await p.click("Explore further")
    await asyncio.sleep(1)
    await p.click("Start the rush")
    await asyncio.sleep(5)
    await p.goto(base + "/proof", 5)
    await p.shot(out / "01-proof.png")
    sections = [("Fast path, with and without", "02-fastpath"), ("What we learned", "03-learned"),
                ("Misses", "04-misses"), ("Your kitchen's shift receipt", "05-receipt"),
                ("What doesn't work yet", "06-limits")]
    for heading, name in sections:
        await p.scroll_to_text(heading)
        await asyncio.sleep(0.5)
        await p.shot(out / f"{name}.png")
    await p.goto(base + "/status", 5)
    await p.shot(out / "07-status.png")
    await p.scroll_to_text("Live AI Ops")
    await asyncio.sleep(0.5)
    await p.shot(out / "08-ops.png")
    await p.goto(base + "/privacy", 3)
    await p.shot(out / "09-privacy.png")
    await p.scroll_to_text("Who else handles your data")
    await asyncio.sleep(0.5)
    await p.shot(out / "10-processors.png")


async def meta(p: Page, base: str, out: Path) -> None:
    """Where the Meta models show: a live story run (real Muse Spark calls, about $0.02 of credit), then the GM plan,
    the Status page with this kitchen's model calls, and what we measured about the models."""
    await p.goto(base + "/", 4)
    await p.shot(out / "01-home.png")
    await p.click("Run the full story live")
    await p.wait_text("Order #", 90)
    await asyncio.sleep(2)
    await p.scroll_to_text("Your order")
    await p.shot(out / "02-order.png")
    await p.wait_text("AI plan:", 150)
    await asyncio.sleep(2)
    await p.scroll_to_text("Playbook, then the AI plan")
    await p.shot(out / "03-plan.png")
    await p.goto(base + "/gm", 5)
    await p.scroll_to_text("AI GM")
    await asyncio.sleep(1)
    await p.shot(out / "04-gm-plan.png")
    await p.goto(base + "/status", 5)
    await p.shot(out / "05-status.png")
    await p.scroll_to_text("Live AI Ops")
    await asyncio.sleep(2)
    await p.shot(out / "06-ops.png")
    await p.goto(base + "/proof", 5)
    await p.scroll_to_text("What we learned")
    await asyncio.sleep(0.5)
    await p.shot(out / "07-learned.png")


async def shelf(p: Page, base: str, out: Path) -> None:
    """Counting stock from a photo (P38): a fresh sample kitchen's GM console, the photo in $SHELF_PHOTO (an absolute
    path) through the real file input (live Muse Spark and SAM 3.1, about $0.01), the review with outlines and both
    counts, then confirm."""
    photo = os.environ["SHELF_PHOTO"]
    await p.goto(base + "/", 4)
    await p.js("fetch('/api/session', {method: 'POST', headers: {'content-type': 'application/json'}, "
               "body: JSON.stringify({passcode: null})}).then((r) => r.status)")  # a paused sample kitchen, no rush
    await p.goto(base + "/gm", 5)
    await p.scroll_to_text("Count stock")
    await asyncio.sleep(1)
    await p.shot(out / "01-panel.png")
    root = (await p.call("DOM.getDocument", depth=-1))["root"]["nodeId"]
    found = await p.call("DOM.querySelector", nodeId=root, selector='input[aria-label="Shelf photo"]')
    await p.call("DOM.setFileInputFiles", files=[photo], nodeId=found["nodeId"])
    await p.wait_text("Counting with Muse Spark and SAM 3.1", 10)
    await p.shot(out / "02-counting.png")
    await p.wait_text("Confirm", 120)
    await asyncio.sleep(1.5)
    await p.shot(out / "03-review.png")
    await p.click("Confirm")
    await p.wait_text("Stock updated", 60)
    await asyncio.sleep(1)
    await p.shot(out / "04-saved.png")
    await p.click("Done")
    await asyncio.sleep(3)
    await p.shot(out / "05-after.png")


SCENARIOS = {"story": story, "kiosk": kiosk, "proof": proof, "meta": meta, "shelf": shelf}


async def main(base: str, scenario: str) -> None:
    out = WORK / "screens" / scenario
    profile = WORK / "chrome" / scenario
    shutil.rmtree(profile, ignore_errors=True)  # a fresh visitor every run: its own sample kitchen
    out.mkdir(parents=True, exist_ok=True)
    async with chrome(profile, width=W, height=H, scale=2, fake_media=True) as p:
        try:
            await SCENARIOS[scenario](p, base.rstrip("/"), out)
        finally:  # stop the sample kitchen's rush so it spends nothing more
            await p.goto(base.rstrip("/") + "/", 3)
            if await p.click("Reset"):
                await asyncio.sleep(1)
                await p.click("Reset the kitchen")
                await asyncio.sleep(2)
    print(f"capture: {scenario} -> {out} ({len(list(out.glob('*.png')))} screens)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base_url")
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    args = ap.parse_args()
    asyncio.run(main(args.base_url, args.scenario))
