"""``POST /api/admin/login`` (CONTRACTS_API §3 #40, P11): a founder's admin token in, the ``ros_admin`` cookie out.

The token is compared in constant time with every entry of ``settings.admin_tokens`` (``vamsi:…,akshitha:…``) and the
cookie records the matching name, which later stamps ``human:<name>`` on DECIDE rows. Attempts are limited to 10 per
15 minutes per client IP (429 with ``Retry-After``); an unknown token is 403 ``forbidden_role``. The other admin routes
(#41, #42, #50) belong to P29 and P30c.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from restaurantos.contracts.api import AdminLoginRequest
from restaurantos.contracts.errors import Forbidden
from restaurantos.security import auth, ratelimit

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/admin/login", status_code=204, response_class=Response)
async def admin_login(body: AdminLoginRequest, request: Request) -> Response:
    settings = auth.settings_of(request)
    ratelimit.limiter().check((ratelimit.ADMIN_LOGIN, ratelimit.ip_key(ratelimit.client_ip(request, settings))))
    name = auth.admin_for_token(settings, body.token)
    if name is None:
        log.warning("admin login refused: unknown token")
        raise Forbidden("Unknown admin token")
    response = Response(status_code=204)
    auth.issue_admin(response, settings, name, auth.now_real_ms(request))
    log.info("admin login: %s", name)
    return response
