"""Goal mode — testable-outcome goals the agent self-drives toward.

See ``graph/goals/controller.py`` for the loop and ``verifiers.py`` for the
pluggable completion checks. Wired into the server invocation paths in
``server.py``; config lives under the ``goal`` block (``graph/config.py``).
"""

from graph.goals.controller import Decision, GoalController
from graph.goals.store import GoalStore
from graph.goals.types import GoalState, VerifyResult

__all__ = ["GoalController", "GoalStore", "GoalState", "VerifyResult", "Decision"]
