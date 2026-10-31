"""P38 shelf count through the real app, lifespan and P11 session cookies, on the fakes (CONTRACTS_API #43,
ARCHITECTURE_VOICE_MEDIA §10): photo → Muse Spark → SAM 3.1 per concept → ``ShelfCount``; a person confirms →
``inventory.adjusted(count)`` with its evidence → ``gm.trigger`` opens a ``count_drift`` incident and runs the planner.

Both kitchens are Spice Route sandboxes at par stock on a paused clock (paneer 6,000 g in 1,000 g packs, eggs 90 ea).
Muse Spark and SAM 3.1 are the scripted ``FakeMetaClient`` (SAM answers are recorded lines, ``recorded.py``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from restaurantos.api.registry import Entry
from restaurantos.app import create_app
from restaurantos.config import Purpose
from restaurantos.contracts import ids
from restaurantos.contracts.common import TenantClockSpec
from restaurantos.contracts.errors import ChaosDown
from restaurantos.gm.trigger import SUBSCRIBES as GM_SUBSCRIBES
from restaurantos.meta.errors import BudgetExceeded, CircuitOpen, MetaAPIError, MetaTimeout
from restaurantos.perception.image import MAX_PHOTO_BYTES
from restaurantos.perception.shelf_count import Turns
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_TTL_MS, SessionClaims
from restaurantos.seed.loader import load_seed
from restaurantos.seed.materialize import config_rows
from restaurantos.services.container import NoopChaos
from restaurantos.services.inventory import SUBSCRIBES as INVENTORY_SUBSCRIBES
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS, FAKE_NOW_MS
from tests.fakes.container import isolated_registry, make_test_container
from tests.perception.recorded import (
    EGGS_LINE,
    GIF,
    JPEG_KEPT,
    PANEER_LINE,
    PNG,
    jpeg,
    responses_answer,
    sam_by_concept,
    sam_input,
    spark_answer,
    spark_item,
)

TID, OTHER = "sbx_shelf_count", "sbx_shelf_other"
PAUSED = TenantClockSpec(mode="sim", anchor_biz_ms=DINNER_RUSH_ANCHOR_MS, anchor_real_ms=FAKE_NOW_MS, speed=0)
SEED = load_seed()
SHELF = "/api/perception/shelf"
KEPT_URL = "data:image/jpeg;base64," + base64.b64encode(JPEG_KEPT).decode()


class RecordingPlanner:
    """Stands in for P23a's planner: records the runs ``gm.trigger`` asks for."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    async def run(self, incident_id: str, *, trigger: str, speculative: bool = False, observe=None) -> None:
        self.runs.append((incident_id, trigger))


class DrillOn:
    """A chaos drill with Meta switched off for every tenant."""

    def check(self, tenant_id, component):
        raise ChaosDown(component, tenant_id)

    def state(self, tenant_id):
        return {"meta": True, "asr": False, "tts": False}


async def seed(c, tenant_id: str) -> None:
    await c.store.add_tenant(tenant_id, clock=PAUSED)
    async with c.store.write() as tx:
        for table, rows in config_rows(SEED, tenant_id):
            await tx.conn.execute(insert(table), rows)


@pytest.fixture
def app(monkeypatch):
    """P12c's provider, routes and handler, P23b's ``gm.trigger`` handler and P38's routes on a fresh container."""
    inventory = "services.inventory"
    isolated_registry(monkeypatch, PROVIDERS=(Entry(inventory, "P12c", "CP1", sets=("inventory",)),),
                      REQUIRED_ROUTERS=(Entry("api.routes_inventory", "P12c", "CP1"),),
                      OPTIONAL_ROUTERS=(Entry("api.routes_perception", "P38", "CP4"),),
                      HANDLERS=(Entry(inventory, "P12c", "CP1", subscribes=INVENTORY_SUBSCRIBES, lane="core"),
                                Entry("gm.trigger", "P23b", "CP2", subscribes=GM_SUBSCRIBES, lane="gm")))
    previous = ids.current()
    c = make_test_container(planner=RecordingPlanner())
    with TestClient(create_app(container=c)) as client:
        client.portal.call(seed, c, TID)
        client.portal.call(seed, c, OTHER)
        sign_in(client, c, "judge")
        yield client, c
    ids.install(previous)


def sign_in(client, c, role: str, tenant_id: str = TID, sid: str = "vs_0123456789ABCDEF") -> None:
    claims = SessionClaims(sid=sid, tid=tenant_id, role=role, lvl="default", pool="judge",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    client.headers["cookie"] = f"{auth.SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"


def upload(client, data: bytes = b"", name: str = "shelf.jpg", **params):
    return client.post(SHELF, files={"image": (name, data or jpeg(), "image/jpeg")}, params=params)


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def script(c, spark: dict, sam: dict | None = None) -> None:
    """One Spark answer, and a SAM answer per concept (an exception instance is raised by that call)."""
    c.meta.add(Purpose.VISION, spark)
    if sam:
        c.meta.add(Purpose.SAM, *[sam_by_concept(sam)] * len(sam))


def adjusted(c) -> list:
    return [e for e in c.hub.events if e.type == "inventory.adjusted"]


def test_confirm_adjusts_and_replans(app):
    client, c = app
    script(c, spark_answer([spark_item("ING_PANEER", "paneer packs", 2), spark_item("ING_EGG", "eggs", 6),
                            spark_item("ING_WATER_BOTTLE", "water bottles", 7)]),
           {"paneer packs": responses_answer(PANEER_LINE), "eggs": responses_answer(EGGS_LINE),
            "water bottles": MetaTimeout("SAM took too long")})
    r = upload(client)
    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("shc_")
    paneer, eggs, water = body["items"]
    assert (paneer["ingredient_id"], paneer["concept"], paneer["spark_count"], paneer["sam_count"], paneer["agreed"],
            paneer["mean_conf_pct"], len(paneer["points"])) == ("ING_PANEER", "paneer packs", 2, 2, True, 90, 2)
    assert paneer["masks_svg"].count("<path d=") == 2
    assert (eggs["spark_count"], eggs["sam_count"], eggs["agreed"]) == (6, 5, True)  # within one: agreement
    assert (water["spark_count"], water["sam_count"], water["agreed"], water["masks_svg"]) == (7, None, False, "")
    sam_calls = c.meta.calls_for(Purpose.SAM, "responses")
    assert sorted(sam_input(call)[0][0] for call in sam_calls) == ["eggs", "paneer packs", "water bottles"]
    assert all(sam_input(call) == ([sam_input(call)[0][0]], [KEPT_URL]) for call in sam_calls)
    assert adjusted(c) == []  # nothing changes until a person confirms

    r = client.post(f"{SHELF}/{body['id']}/confirm", json={"counts": {"ING_PANEER": 2, "ING_EGG": 30}})
    assert r.status_code == 200
    views = {v["ingredient"]["id"]: v for v in r.json()}
    assert views["ING_PANEER"]["ingredient"]["on_hand"] == 2000  # 2 packs × 1,000 g
    assert views["ING_EGG"]["ingredient"]["on_hand"] == 30  # eggs count one by one
    assert {"band", "free", "formula"} <= set(views["ING_PANEER"]["forecast"])
    events = {e.payload["ingredient_id"]: e for e in adjusted(c)}
    photo_sha = hashlib.sha256(JPEG_KEPT).hexdigest()  # the stripped bytes Meta saw; the photo itself is gone
    paneer_event, egg_event = events["ING_PANEER"], events["ING_EGG"]
    assert (paneer_event.payload["source"], paneer_event.payload["from_qty"], paneer_event.payload["to_qty"]) == (
        "count", 6000, 2000)
    assert paneer_event.payload["evidence"] == {"photo_sha256": photo_sha, "spark_count": 2, "sam_count": 2,
                                                "agreed": True}
    assert paneer_event.actor == "judge"
    assert (egg_event.payload["source"], egg_event.payload["from_qty"], egg_event.payload["to_qty"]) == (
        "count", 90, 30)
    assert egg_event.payload["evidence"] == {"photo_sha256": photo_sha, "spark_count": 6, "sam_count": 5,
                                             "agreed": True}
    assert "ING_WATER_BOTTLE" not in events  # left out of the confirm: untouched

    client.portal.call(c.bus.drain)  # gm.trigger: a count far below the books opens count_drift and plans
    rows = client.portal.call(incident_rows, c)
    assert {(kind, ingredient) for kind, ingredient in rows} == {("count_drift", "ING_PANEER"),
                                                                 ("count_drift", "ING_EGG")}
    assert c.planner.runs and {trigger for _, trigger in c.planner.runs} == {"act"}
    again = client.post(f"{SHELF}/{body['id']}/confirm", json={"counts": {"ING_WATER_BOTTLE": 7}})
    assert error(again) == (404, "not_found")  # a count is confirmed once


async def incident_rows(c) -> list[tuple[str, str]]:
    async with c.store.read() as conn:
        rows = await conn.execute(select(t.incidents.c.kind, t.incidents.c.ingredient_id).where(
            t.incidents.c.tenant_id == TID))
        return [(row.kind, row.ingredient_id) for row in rows]


def test_files_swept(app):
    """Pending counts wait 15 minutes (real time), swept on every access; nothing about the photo but its hash."""
    client, c = app
    service = client.app.state.shelf_count if hasattr(client.app.state, "shelf_count") else None
    assert service is None  # made on first use
    script(c, spark_answer([spark_item("ING_EGG", "eggs", 5)]), {"eggs": responses_answer(EGGS_LINE)})
    first = upload(client).json()["id"]
    c.clock.advance(minutes=10)
    script(c, spark_answer([spark_item("ING_EGG", "eggs", 5)]), {"eggs": responses_answer(EGGS_LINE)})
    second = upload(client).json()["id"]
    pending = client.app.state.shelf_count.pending
    assert len(pending) == 2
    [kept] = [count for count in pending._by_tenant[TID].values() if count.id == first]
    assert len(kept.photo_sha256) == 64 and not hasattr(kept, "data")  # the photo itself is never kept
    c.clock.advance(minutes=5, seconds=1)  # the first is 15 min 1 s old, the second 5 min 1 s
    assert error(client.post(f"{SHELF}/{first}/confirm", json={"counts": {"ING_EGG": 5}})) == (404, "not_found")
    assert len(pending) == 1
    assert client.post(f"{SHELF}/{second}/confirm", json={"counts": {"ING_EGG": 5}}).status_code == 200
    assert len(pending) == 0
    c.clock.advance(minutes=20)
    assert pending.sweep() == 0 and pending._by_tenant == {}


def test_upload_validation_type_size_exif(app):
    client, c = app
    assert error(upload(client, GIF, "shelf.gif")) == (400, "bad_request")
    assert error(upload(client, b"%PDF-1.7 not a photo", "shelf.jpg")) == (400, "bad_request")  # the name is ignored
    assert error(upload(client, jpeg()[:40])) == (400, "bad_request")  # a truncated JPEG
    assert error(client.post(SHELF, json={"image": "aGVsbG8="})) == (400, "bad_request")  # not multipart
    assert error(client.post(SHELF, files={"photo": ("shelf.jpg", jpeg(), "image/jpeg")})) == (400, "bad_request")
    assert error(upload(client, PNG + bytes(MAX_PHOTO_BYTES), "big.png")) == (413, "payload_too_large")
    huge = client.post(SHELF, files={"image": ("huge.png", PNG + bytes(MAX_PHOTO_BYTES + 128 * 1024), "image/png")})
    assert error(huge) == (413, "payload_too_large")  # refused on Content-Length, before reading
    assert c.meta.calls == []  # refused uploads never reach Meta (and never use up the photo limit)

    script(c, spark_answer([spark_item("ING_EGG", "eggs", 5)]), {"eggs": responses_answer(EGGS_LINE)})
    assert upload(client, jpeg()).status_code == 200
    [spark_call] = c.meta.calls_for(Purpose.VISION)
    [_, picture] = spark_call.kwargs["input"][0]["content"]
    assert picture["image_url"] == KEPT_URL  # EXIF, XMP, IPTC, the comment and the thumbnail never left the server
    assert b"GPSLatitude" not in base64.b64decode(picture["image_url"].split(",", 1)[1])


def test_photo_limit_429(app):
    client, c = app
    for _ in range(6):
        script(c, spark_answer([]))
        assert upload(client).json()["items"] == []  # nothing countable in sight is a normal answer
    refused = upload(client)
    assert error(refused) == (429, "rate_limited")
    assert refused.json()["error"]["details"] == {"limit": "shelf_photo", "max": 6, "window_s": 600,
                                                  "fallback": "manual"}
    assert "6 photos every 10 minutes" in refused.json()["error"]["message"]
    assert int(refused.headers["retry-after"]) == 600
    assert len(c.meta.calls_for(Purpose.VISION)) == 6  # the refusal spent nothing
    sign_in(client, c, "judge", sid="vs_ANOTHER0SESSION01")  # the limit is per session
    script(c, spark_answer([]))
    assert upload(client).status_code == 200
    sign_in(client, c, "judge")
    c.clock.advance(minutes=10)
    script(c, spark_answer([]))
    assert upload(client).status_code == 200  # the window slid past the first photos


def test_tenant_isolation(app):
    client, c = app
    script(c, spark_answer([spark_item("ING_PANEER", "paneer packs", 2)]),
           {"paneer packs": responses_answer(PANEER_LINE)})
    count_id = upload(client).json()["id"]
    confirm = f"{SHELF}/{count_id}/confirm"
    body = {"counts": {"ING_PANEER": 2}}
    for role in ("guest", "cook", "foh"):  # M: a manager, a judge in its own sandbox, or an admin —
        sign_in(client, c, role)  # or, since pass 13, a staff sign-in holding Inventory Manager
        assert error(upload(client)) == (403, "inventory_access")
        assert error(client.post(confirm, json=body)) == (403, "inventory_access")
    sign_in(client, c, "judge", OTHER)
    assert error(client.post(confirm, json=body)) == (404, "not_found")  # another kitchen's count is unknown here
    assert error(client.post(confirm, json=body, params={"tenant": TID})) == (403, "wrong_tenant")
    assert error(upload(client, tenant=TID)) == (403, "wrong_tenant")
    client.headers.pop("cookie")
    assert error(upload(client)) == (401, "no_session")
    assert adjusted(c) == []

    sign_in(client, c, "judge")
    assert error(client.post(confirm, json={"counts": {}})) == (400, "bad_request")
    unknown = client.post(confirm, json={"counts": {"ING_EGG": 3}})
    assert error(unknown) == (400, "bad_request") and unknown.json()["error"]["details"]["unknown"] == ["ING_EGG"]
    for bad in (-1, 1000):
        assert error(client.post(confirm, json={"counts": {"ING_PANEER": bad}})) == (400, "bad_request")
    assert client.post(confirm, json={"counts": {"ING_PANEER": "lots"}}).status_code == 422
    assert client.post(confirm, json=body).json()[0]["ingredient"]["on_hand"] == 2000  # refusals kept the count
    sign_in(client, c, "judge", OTHER)
    other = client.get("/api/inventory").json()
    assert next(v for v in other if v["ingredient"]["id"] == "ING_PANEER")["ingredient"]["on_hand"] == 6000


@pytest.mark.parametrize(("failure", "reason"), [
    (BudgetExceeded("scope", 0.4, 0.4, spend_scope=TID), "budget"),
    (CircuitOpen("vision breaker open"), "circuit_open"),
    (MetaTimeout("no answer in 60 s"), "timeout"),
    (MetaAPIError(503, "service_overloaded"), "upstream_error"),
    (responses_answer("I can't count stock."), "unreadable"),
    (responses_answer('{"items": [{"ingredient_id": "ING_EGG", "concept": "eg', status="incomplete"), "unreadable"),
])
def test_meta_unavailable_503(app, failure, reason):
    client, c = app
    c.meta.add(Purpose.VISION, failure)
    r = upload(client)
    assert error(r) == (503, "ai_unavailable")
    assert r.json()["error"]["details"] == {"component": "meta", "fallback": "manual", "reason": reason}
    assert "Enter the count by hand" in r.json()["error"]["message"]
    assert ("retry-after" in r.headers) == (reason == "circuit_open")
    assert c.meta.calls_for(Purpose.SAM) == [] and adjusted(c) == []


def test_chaos_bad_image_and_sam_down(app):
    client, c = app
    c.chaos = DrillOn()
    r = upload(client)
    assert error(r) == (503, "ai_unavailable") and r.json()["error"]["details"]["reason"] == "chaos"
    assert c.meta.calls == []  # the drill answers before any model call
    c.chaos = NoopChaos()  # the drill is over

    c.meta.add(Purpose.VISION, MetaAPIError(400, "the image could not be decoded"))
    r = upload(client)
    assert error(r) == (400, "bad_request") and r.json()["error"]["details"]["reason"] == "bad_image"

    script(c, spark_answer([spark_item("ING_PANEER", "paneer packs", 2), spark_item("ING_EGG", "eggs", 5)]),
           {"paneer packs": BudgetExceeded("scope", 0.4, 0.4), "eggs": responses_answer(EGGS_LINE, "incomplete")})
    r = upload(client)
    assert r.status_code == 200  # SAM down costs only SAM's counts: Spark's stand, never as agreement
    assert [(i["spark_count"], i["sam_count"], i["agreed"]) for i in r.json()["items"]] == [(2, None, False),
                                                                                            (5, None, False)]


def rows(r) -> list[tuple]:
    return [(i["ingredient_id"], i["concept"], i["spark_count"], i["sam_count"], i["agreed"])
            for i in r.json()["items"]]


def test_shared_phrase_is_never_one_count_for_two(app):
    """Spark calls paneer and butter both "white packs": SAM would count both kinds under it, so each asks SAM for its
    own name; a phrase two items still share gets no SAM call, never a false agreement."""
    client, c = app
    script(c, spark_answer([spark_item("ING_PANEER", "white packs", 4), spark_item("ING_BUTTER", "white packs", 1)]),
           {"paneer": responses_answer(EGGS_LINE), "butter": responses_answer(PANEER_LINE)})
    r = upload(client)
    assert rows(r) == [("ING_PANEER", "paneer", 4, 5, True), ("ING_BUTTER", "butter", 1, 2, True)]
    assert sorted(sam_input(call)[0][0] for call in c.meta.calls_for(Purpose.SAM)) == ["butter", "paneer"]

    script(c, spark_answer([spark_item("ING_PANEER", "white packs", 2), spark_item("ING_BUTTER", "white packs", 1),
                            spark_item("ING_EGG", "paneer", 5)]), {"butter": responses_answer(PANEER_LINE)})
    r = upload(client)
    assert rows(r) == [("ING_PANEER", "paneer", 2, None, False), ("ING_BUTTER", "butter", 1, 2, True),
                       ("ING_EGG", "paneer", 5, None, False)]
    assert [sam_input(call)[0][0] for call in c.meta.calls_for(Purpose.SAM)][2:] == ["butter"]


async def test_turns_one_photo_per_session_and_two_at_once():
    turns, gate, seen = Turns(slots=2), asyncio.Event(), []

    async def photo(session: str, name: str) -> None:
        async with turns.turn(session):
            seen.append(name)
            await gate.wait()

    tasks = [asyncio.create_task(photo(session, name)) for session, name in (("s1", "a"), ("s1", "b"), ("s2", "c"),
                                                                             ("s3", "d"))]
    for _ in range(5):
        await asyncio.sleep(0)
    assert seen == ["a", "c"]  # b waits for its session's first photo, d for one of the two slots
    gate.set()
    await asyncio.gather(*tasks)
    assert sorted(seen) == ["a", "b", "c", "d"] and len(turns) == 0  # every session's lock went with its last photo
