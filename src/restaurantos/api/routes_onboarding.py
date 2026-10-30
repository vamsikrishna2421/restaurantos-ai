"""Onboarding routes (P39): a new restaurant drafts itself from its menu, then goes live.

| Route | Auth | Does |
|---|---|---|
| POST /api/onboarding/draft | S | fetch the public Toast page (or read pasted menu text) → the draft |
| POST /api/onboarding/go-live | S | write the reviewed draft as a tenant config (validated), then activate it |

The draft step is the whole automation: menu, station placement, station sizes and the shift's job slots
arrive as one proposal for the review screen (``services.onboarding``). Fetch and parse failures are honest,
structured errors — ``menu_fetch_failed`` (502) when the page cannot be opened, ``menu_unparseable`` (422)
when it opens but holds nothing menu-shaped; the UI shows them and offers the paste fallback. Go-live never
overwrites: a name whose tenant id is taken answers 409 ``tenant_exists``.

Go-live is a real switch, not a file drop: after the config is written it materializes the new tenant's seed
in the running app (``seed.materialize.activate_tenant`` — the tenant becomes the deployment's template and
``live``/``demo`` are rebuilt from it) and records it as the active tenant
(``seed.loader.set_active_tenant``), so the next Start, staff sign-in or kiosk session serves the new
restaurant with no restart. Devices already mid-shift keep the sandbox they cloned. If activation itself
fails, the answer is an honest 500 ``activation_failed`` — the config stays on disk and the app keeps
serving the previous restaurant; nothing claims the new one is live when it is not.
"""

from __future__ import annotations

from fastapi import APIRouter

from restaurantos.api.deps import ContainerDep
from restaurantos.contracts.api import (
    OnboardingDraft,
    OnboardingDraftRequest,
    OnboardingGoLiveRequest,
    OnboardingGoLiveResult,
)
from restaurantos.contracts.errors import BadRequest, NotWired
from restaurantos.security.auth import SessionAuth
from restaurantos.seed import loader, materialize
from restaurantos.seed.validate import SeedError
from restaurantos.services import onboarding as ob

router = APIRouter(tags=["onboarding"])


@router.post("/api/onboarding/draft", response_model=OnboardingDraft)
async def make_draft(body: OnboardingDraftRequest, who: SessionAuth) -> OnboardingDraft:
    """Build the proposal from the one source the manager gave. Exactly one source must be set."""
    name = body.restaurant_name.strip()
    if not name:
        raise BadRequest("Give the restaurant a name", code="bad_request")
    if body.toast_url and body.menu_text:
        raise BadRequest("Give one source: a Toast link or pasted menu text, not both", code="bad_request")
    if body.toast_url:
        url = body.toast_url.strip()
        if not url.lower().startswith(("https://", "http://")):
            raise BadRequest("That link does not look like a web page address", code="bad_request")
        html = await ob.fetch_menu_page(url)
        return ob.draft_from_html(name, html)
    if body.menu_text:
        return ob.draft_from_text(name, body.menu_text)
    raise BadRequest("Give a Toast menu link, or paste the menu text", code="bad_request")


@router.post("/api/onboarding/go-live", response_model=OnboardingGoLiveResult)
async def go_live(body: OnboardingGoLiveRequest, who: SessionAuth, c: ContainerDep) -> OnboardingGoLiveResult:
    """Write the reviewed draft as a tenant config directory, then activate it in the running app.
    409 ``tenant_exists`` when the name's tenant id is already set up; 400 ``draft_invalid`` (with the
    problems) when the draft fails the tenant checks; 500 ``activation_failed`` when the written tenant
    cannot be activated (the config stays on disk; the previous restaurant keeps serving)."""
    slug, files = ob.publish_tenant(body.draft, body.manager_name)
    if c.store is None:
        raise NotWired("store")
    try:
        await materialize.activate_tenant(c.store, slug, tenants_dir=ob.tenants_dir(),
                                          now_real_ms=c.clock.now_real_ms())
    except SeedError as exc:
        raise ob.ActivationFailed(
            f"{body.draft.restaurant_name} is set up on disk but could not be activated",
            details={"tenant_id": slug, "problems": exc.problems}) from exc
    loader.set_active_tenant(c.settings.data_dir, slug)
    return OnboardingGoLiveResult(tenant_id=slug, name=body.draft.restaurant_name, files=files, status="live")
