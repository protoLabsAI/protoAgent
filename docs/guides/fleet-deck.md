# The fleet deck — the fleet in a terminal

The **fleet deck** is a terminal UI over the running hub: every member of the fleet on one
screen, what each is doing right now, and the controls to talk to them, answer them, and manage
them — without opening the browser console. It is the same fleet the console shows, read and
driven through the same hub API.

```bash
uv tool install protolabs-agent   # once; or: pipx install protolabs-agent
protoagent fleet                  # or: protoagent top
```

The `protoagent` command comes from the `protolabs-agent` package on PyPI (see
[Install](./cli.md#install)). A source checkout doesn't install it: there, run
`uv run python -m server fleet`. With only the desktop app installed, use
[its bundled binary](#from-the-desktop-app). `q` quits the deck; members
keep running. The footer only ever lists the keys that apply to the selected row, and `?` lists
all of them — the full key table is in [the CLI guide](./cli.md#the-fleet-deck-protoagent-fleet-with-no-arguments).

The deck needs a terminal. For scripts, use the non-interactive verbs with `--json`
(`protoagent fleet ls --json`, `protoagent fleet --all --json`).

## See what the fleet is doing

The **roster** lists every member with the console's presence words — `host`, `online`,
`remote`, `stopped`, `unreachable` — its version (a skew against the hub is marked), 24-hour
spend, and a **TURN** column: `⟳` while a member is working, `⚑` when a turn is parked waiting
for you. The hub's own warnings show as a banner above the roster.

- `i` opens **member detail**: runtime status (model, identity, warnings), a following tail of
  the member's log, its sessions, and its telemetry rollup. Each pane degrades on its own if its
  read fails.
- `w` opens the **work feed**: tool calls, room replies, spend and parked questions from every
  online member, folded into one time-ordered feed. `f` filters, `p` pauses, `enter` opens that
  member's conversation at the row's session. The deck rings the terminal bell when a member
  newly needs you.
- `s`, `x`, `r` start, stop and restart the selected member; `l` follows its log; `o` opens it
  in the browser console.

## Talk to a member

`enter` (or `c`) on an online member opens a **conversation**. Your messages and the member's
answers stream into the transcript; the WORK pane lists each tool call of the current turn as it
happens — a subagent's own calls nested under its `task` card — and `enter` on a card shows its
full arguments and result.

Sessions are **shared with the console**: a conversation started in the deck is waiting in the
browser, and the other way round. `ctrl+s` lists the member's sessions and replays one, tool
cards included; `ctrl+n` starts a new one.

## Answer a member that is waiting on you

When a turn parks on a question, a form or an approval, the roster shows `⚑` and the
conversation's status line says what it needs.

- `enter` on the empty composer (or `ctrl+r`) opens the request. A plain question also takes
  whatever you type as the answer.
- Approvals are `a` / `d`. A form is a stepped wizard (`ctrl+→` / `ctrl+←`, `ctrl+s` submits)
  with the console's rules for required fields, choices and conditional fields.
- `ctrl+d` dismisses a request the way the console does, so the turn never stays parked forever.

While a member is working, typing **steers** the running turn: the message queues and folds in at
the member's next model call. `ctrl+x` on a running `task` card cancels that one delegation, not
the turn. A turn the scheduler or an inbox started is attached too when you open its session, and
when the member marks it operator-controllable the composer interjects into it; `esc` on an
attached turn detaches and never cancels someone else's work.

## Manage members

On a live hub, from the roster:

| Key | Does |
|---|---|
| `n` | Create a member: a name, an archetype from the hub's catalog, whether it inherits the hub's model connections (on by default), whether to start it |
| `R` | Rename the display name only — the id, URL slug and data never change |
| `d` | Delete: the member is stopped first, you type its name to confirm, and purging its data is a separate choice |
| `a` / `e` | Register a remote protoAgent, or edit one in place (the bearer is typed masked and never shown again) |
| `J` / `K` | Move the selected row; the order is saved on the hub |

The same operations are `protoagent fleet new | rm | rename | remote add|edit|rm | order` on the
command line, and the `fleet.*` operations on the shared ops layer.

## Every hub on the box

`H` (or `protoagent fleet --all`) lists every hub this machine runs — the desktop app's,
`~/.protoagent`, each scoped instance under it — and protoAgents found on this box's ports and the
tailnet. Each row shows its state, how it was launched, its port, version and member counts.

- `enter` on a running hub **attaches** the deck to it: the roster, the feed and conversations
  then belong to that fleet.
- `u` on a stopped hub runs `protoagent up` for that instance root and attaches once it answers.
  It comes up on the port it last used when that port is free and no member of any instance on
  the box records it, else on the first such port from 7870 to 7910.
- Stopping a hub is not a deck action — run `protoagent down` in that instance
  (`PROTOAGENT_HOME=<instance root> protoagent down`). A plain `protoagent fleet down` finds the
  hub that answers first, which may be the desktop app's.

A hub that answers but refuses every credential reads `unauthorized`; one that does not answer
reads `unreachable`.

## Credentials and hubs elsewhere

On this machine the deck opens a hub with that hub's own fleet service token. For a hub
elsewhere, name it: `protoagent fleet --hub https://host:7870 --token …` (or
`PROTOAGENT_HUB_TOKEN`). Local tokens are only ever sent to loopback hubs, no credential goes
off the box over plain `http://` unless you pass `--insecure-http` for a link you know is
encrypted underneath (a tailnet), and a peer the network reported is never sent any credential —
its name is its own claim.

## Offline

With no hub answering, the deck shows this instance's `fleet.json`, badged `offline`, and only
start and stop are available. `H` still lists every hub on the box, and `u` brings one up. If a
hub *did* answer but refused this shell, `protoagent fleet --all` still opens the hub tree; the
roster beneath it drives nothing from disk beside that hub — attach to it from the tree, or pass
`--token`.

## From the desktop app

From 0.166.0, the desktop app's bundled binary opens the deck too, which matters on a machine
with no other `protoagent` installed. On macOS:

```bash
/Applications/protoAgent.app/Contents/MacOS/protoagent-server fleet
```

## Troubleshooting

| You see | Why | Do |
|---|---|---|
| `protoagent: command not found` | a source checkout and the desktop app don't put `protoagent` on your PATH | `uv tool install protolabs-agent`; in a checkout, `uv run python -m server fleet`; with only the desktop app, [its bundled binary](#from-the-desktop-app) |
| `the deck needs a terminal` | stdout or stdin is not a TTY | use `protoagent fleet ls --json` in scripts |
| `the fleet deck is not available in this build` | this binary was built without Textual | use `protoagent fleet ls \| up \| down`, or run from a source checkout |
| a hub reads `unauthorized` | it answered but refused every credential this shell has | attach from `H` if it is on this box, else `--hub … --token …` |
| the roster is badged `offline` beside a running hub | the hub refused this shell | `H` and attach, or pass `--token` |
| a member exits at start with `EADDRINUSE` | another instance's member records the same port | see [port collisions](./operating-a-fleet.md#port-collision-between-instances) |

## How it fits

The deck is a separate process and an HTTP client of the hub: it reads members through the hub's
slug proxy ([ADR 0042](../adr/0042-fleet-supervisor-unified-console.md)) and speaks the same A2A
and `/api` surface as the console. It adds no server surface of its own. Putting conversations in
the terminal is the ADR 0075 amendment
([external interfaces](../adr/0075-external-interfaces-cli-mcp-api.md)).
