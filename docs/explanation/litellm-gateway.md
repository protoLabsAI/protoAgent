# LiteLLM gateway

A LiteLLM gateway is an optional way to share model routing across a fleet.
protoAgent also supports direct OpenAI-compatible connections and native
subscription connections. Use [Model connections](/guides/model-connections)
for setup and a successful completion test; this page explains when a gateway
can help.

## The problem this solves

A gateway can give several agents one endpoint and centralize model aliases,
credentials, and routing policy. Each agent selects a configured connection and
model; the gateway maps that request to a provider. This adds a service to operate.
A single agent can use a direct connection without deploying a gateway.

## The alias pattern

A name such as `protolabs/reasoning` is a gateway alias, not a provider model ID.
Configure that alias on your gateway, then select it through the agent's gateway
connection. Changing the alias's provider mapping can move callers together.
Use separate aliases when agents need different routing or rollout policies.

After changing a mapping, run a small completion through each affected agent.
A listed model proves discovery, not that authentication and generation work.

## Why OpenAI-compatible specifically

The compatible endpoint lets protoAgent use its gateway connection with the
configured base URL. Native subscription connections follow their own provider
path; they do not require a LiteLLM endpoint.

## What you trade off

The gateway adds a network hop and another availability dependency. Provider
features and usage reporting depend on what the gateway and selected connection
support. Test the features your workflow needs instead of assuming every provider
feature survives translation. Choose the supported connection in Settings rather
than adding an ad hoc SDK bypass to ordinary agent code.

## What about `usage_metadata`?

Usage reporting feeds local telemetry and cost estimates. Availability depends on
the model connection and streaming response; a missing usage count is not proof
that the request was free. See [Cost and trace](/explanation/cost-and-trace).

## What about cost tracking at the gateway?

protoAgent accumulates turn usage and computes cost estimates in-process. Its
A2A `cost-v1` payload uses URI-keyed **metadata**, not a renderable DataPart.
Gateway accounting is a separate view; see the [wire contract](/reference/extensions#cost-v1).

## Why not just use an OpenAI key directly?

A direct compatible connection is a supported choice. Use a gateway when shared
routing or administration is useful enough to justify operating it; use a direct
or native connection when it meets your needs. Switching configured connections
and models does not inherently require a code change or deployment.

## Related

- [Model connections](/guides/model-connections) — configure and verify access
- [Configuration reference](/reference/configuration) — saved connection/model fields
- [Cost and trace](/explanation/cost-and-trace) — local usage and propagation
