"""Named kill points for the interruption harness.

When LAB_KILL_AT names a point, reaching it ends the process at once with exit code 137, the way
`kill -9` would: no cleanup, no flush beyond what was already written. Outside the harness the
variable is unset and these are no-ops.
"""
import os

POINTS = ("after_hypothesis", "mid_work", "after_approval", "mid_effect", "between_effects",
          "after_effects")


def point(name: str) -> None:
    if os.environ.get("LAB_KILL_AT") == name:
        os._exit(137)
