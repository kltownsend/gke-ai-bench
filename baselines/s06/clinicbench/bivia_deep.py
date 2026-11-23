"""Bivia in deep mode (Keith, 2026-11-09: "Add it to the queue").

Bivia's API documents `mode: "deep"` as deciding, then challenging the decision before committing,
and abstaining if it's still unresolved. The zero-shot reader runs `fast`, Bivia's default. This
subclass changes only the mode; the request, abstain handling and logging are the zero-shot
reader's (clinicbench/bivia_model.py, unchanged). The runner picks it with
--reader bivia --bivia-mode deep.
"""

from __future__ import annotations

from .bivia_model import BiviaModel


class BiviaDeep(BiviaModel):
    MODE = "deep"
