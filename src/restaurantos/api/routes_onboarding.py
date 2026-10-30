"""Onboarding routes (P39): a new restaurant drafts itself from its menu, then goes live as a tenant config.

| Route | Auth | Does |
|---|---|---|
| POST /api/onboarding/draft | S | fetch the public Toast page (or read pasted menu text) → the draft |
| POST /api/onboarding/go-live | S | write the reviewed draft as ``config/tenants/<slug>/`` (validated first) |

The draft step is the whole automation: menu, station placement, station sizes and the shift's job slots
arrive as one proposal for the review screen (``services.onboarding``). Fetch and parse failures are honest,
structured errors — ``menu_fetch_failed`` (502) when the page cannot be opened, ``menu_unparseable`` (422)
when it opens but holds nothing menu-shaped; the UI shows them and offers the paste fallback. Go-live never
overwrites: a name whose tenant id is taken answers 409 ``tenant_exists``.

What go-live delivers today is the validated tenant config directory in exactly the format ``seed.loader``
reads. The loader currently materializes only the template tenant, so the new restaurant is fully set up on
disk but not yet served by the running app — see ``services.onboarding``'s docstring; nothing here pretends
otherwise (no fake "live" tenant is minted).
"""

from __future__ import annotations

from fastapi import APIRouter

from restaurantos.contracts.api import (
    OnboardingDraft,
    OnboardingDraftRequest,
    OnboardingGoLiveRequest,
    OnboardingGoLiveResult,
)
from restaurantos.contracts.errors import BadRequest
from restaurantos.security.auth import SessionAuth
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
async def go_live(body: OnboardingGoLiveRequest, who: SessionAuth) -> OnboardingGoLiveResult:
    """Write the reviewed draft as a tenant config directory. 409 ``tenant_exists`` when the name's tenant
    id is already set up; 400 ``draft_invalid`` (with the problems) when the draft fails the tenant checks."""
    slug, files = ob.publish_tenant(body.draft, body.manager_name)
    return OnboardingGoLiveResult(tenant_id=slug, name=body.draft.restaurant_name, files=files)
