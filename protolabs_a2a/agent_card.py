"""protoLabs A2A agent-card conventions, on top of ``a2a-sdk``'s AgentCard.

``build_agent_card`` assembles an A2A 1.0 ``AgentCard`` (proto) applying the
fleet conventions every protoLabs agent shares:

  - ``supported_interfaces[]`` declares a JSONRPC interface at the agent's
    ``/a2a`` URL with ``protocol_version = "1.0"``.
  - ``provider = {organization: "protoLabs AI", url: "https://protolabs.ai"}``.
  - the four custom extensions are declared in ``capabilities.extensions[]``
    (URIs from ``extensions.py``) so consumers know to extract their DataParts.
  - the auth schemes (``apiKey`` always, ``bearer`` when a token is configured)
    are advertised in ``security_schemes`` + ``security_requirements``.

Callers pass the agent-specific bits (name, description, skills); the
conventions are applied uniformly. The agent owns WHICH extensions it actually
emits — pass ``extension_uris`` to declare only those.
"""

from __future__ import annotations

from collections.abc import Iterable

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol

from .auth import security_requirements, security_schemes
from .extensions import ALL_EXTENSION_URIS

PROVIDER_ORGANIZATION = "protoLabs AI"
PROVIDER_URL = "https://protolabs.ai"


def protolabs_provider() -> AgentProvider:
    """The shared fleet provider block."""
    return AgentProvider(organization=PROVIDER_ORGANIZATION, url=PROVIDER_URL)


def jsonrpc_interface(url: str) -> AgentInterface:
    """A JSONRPC ``supported_interfaces`` entry at ``url``, protocol 1.0."""
    return AgentInterface(
        url=url,
        protocol_binding=TransportProtocol.JSONRPC.value,
        protocol_version=PROTOCOL_VERSION_1_0,
    )


def build_agent_card(
    *,
    name: str,
    description: str,
    url: str,
    version: str,
    skills: Iterable[AgentSkill],
    extension_uris: Iterable[str] = ALL_EXTENSION_URIS,
    bearer: bool = False,
    streaming: bool = True,
    push_notifications: bool = True,
    default_input_modes: Iterable[str] = ("text/plain",),
    default_output_modes: Iterable[str] = ("text/markdown",),
) -> AgentCard:
    """Build a 1.0 AgentCard with the protoLabs conventions applied.

    Args:
        name / description / version: agent identity.
        url: the JSON-RPC endpoint (where ``message/send`` is accepted),
            e.g. ``http://{host}/a2a`` — advertised both as the interface url.
        skills: the agent's ``AgentSkill`` list (must be non-empty to be usable).
        extension_uris: the custom-extension URIs this agent emits; declared in
            ``capabilities.extensions``. Defaults to all four protoLabs ones.
        bearer: advertise the bearer scheme (a token is configured).
        streaming / push_notifications: capability flags (honest — the runtime
            must actually support them).
    """
    return AgentCard(
        name=name,
        description=description,
        version=version,
        provider=protolabs_provider(),
        supported_interfaces=[jsonrpc_interface(url)],
        capabilities=AgentCapabilities(
            streaming=streaming,
            push_notifications=push_notifications,
            extensions=[AgentExtension(uri=uri) for uri in extension_uris],
        ),
        security_schemes=security_schemes(bearer=bearer),
        security_requirements=security_requirements(bearer=bearer),
        default_input_modes=list(default_input_modes),
        default_output_modes=list(default_output_modes),
        skills=list(skills),
    )
