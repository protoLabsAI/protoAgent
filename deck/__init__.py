"""The fleet deck — protoAgent's terminal surface for a RUNNING fleet (epic #3466).

Layout (slices land here one at a time):

- ``deck.hub`` — the hub client: find a live hub on this box (or ``--hub URL``), resolve a
  credential for it, read the roster, drive lifecycle over ``/api/fleet``. (#3467)
- the Textual application (roster, member detail, conversations, work feed) arrives in
  later slices and is lazy-imported by the ``fleet`` dispatcher so ``protoagent --help``
  never pays for it.

Neutral by contract: this package talks HTTP to a hub and never imports ``server`` or
``operator_api`` (it is listed with the infra packages in the import-linter contract), and
it does not import ``graph`` either — ``graph/fleet/cli.py`` imports *this*, so the deck
stays a separate process from the runtime it manages.
"""
