"""protoLabs A2A auth conventions — the fleet's two security schemes.

A protoLabs agent advertises at most two auth schemes on its card:

  - ``apiKey``  — an ``X-API-Key`` request header (always advertised).
  - ``bearer``  — an HTTP ``Authorization: Bearer <token>`` scheme (advertised
                  only when a bearer token is configured).

These builders return ``a2a-sdk`` proto ``SecurityScheme`` objects + the
matching ``SecurityRequirement`` entries, so ``agent_card.build_agent_card``
can drop them straight onto the card. ``a2a-sdk`` owns the request-time
enforcement; this module only declares the schemes.
"""

from __future__ import annotations

from a2a.types import (
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

API_KEY_SCHEME_NAME = "apiKey"
BEARER_SCHEME_NAME = "bearer"
API_KEY_HEADER = "X-API-Key"


def api_key_scheme(header: str = API_KEY_HEADER) -> SecurityScheme:
    """An apiKey-in-header security scheme (``X-API-Key`` by default)."""
    return SecurityScheme(
        api_key_security_scheme=APIKeySecurityScheme(location="header", name=header)
    )


def bearer_scheme() -> SecurityScheme:
    """An HTTP bearer security scheme."""
    return SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer"))


def security_schemes(*, bearer: bool) -> dict[str, SecurityScheme]:
    """Build the card's ``security_schemes`` map.

    Always includes ``apiKey``; adds ``bearer`` when ``bearer`` is True
    (a token is configured).
    """
    schemes = {API_KEY_SCHEME_NAME: api_key_scheme()}
    if bearer:
        schemes[BEARER_SCHEME_NAME] = bearer_scheme()
    return schemes


def security_requirements(*, bearer: bool) -> list[SecurityRequirement]:
    """Build the card's ``security_requirements`` list (OR-alternatives).

    ``apiKey`` is always a permitted scheme; ``bearer`` is added as an
    alternative when configured.
    """
    reqs = [SecurityRequirement(schemes={API_KEY_SCHEME_NAME: StringList(list=[])})]
    if bearer:
        reqs.append(SecurityRequirement(schemes={BEARER_SCHEME_NAME: StringList(list=[])}))
    return reqs
