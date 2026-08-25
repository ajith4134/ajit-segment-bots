"""Learned state that survives the off switch.

The mechanism is `runtime/durable_state.py` -- atomic write, settings-guarded
restore, schema check -- and it is general, because a lot book has to survive a
restart for reasons that have nothing to do with learning. This module is that
mechanism named for the use that first needed it, and it keeps the measured
story of why, below.

`LearnedStateStore` is `DurableStateStore`. The name is kept because every
learning part reads `learned_state_root`, and renaming the setting would move
the directory and start all five of them cold -- the bull model alone would
lose 38,219 labels to a rename that bought nothing.

**Why this is substrate and not a part (RL-069).** A part is a process the governor
may SIGKILL at any moment (T-3), and one that keeps what it learned only in that
process learns nothing across a day. The place a learned component's coefficients
live is the same kind of question as where the tape lives or where settings live:
below the diagram, available to every part, wired to none.

The failure this closes was measured, not imagined. `bull-conviction-model` built
`OnlineLogisticModel` fresh in `start_part` on every fork. Its `is_fitted` needs
`bull_minimum_training_observations` labelled outcomes **of each class**; every
restart of the spine put that count back to zero, so the bot could accumulate
labels for an hour, be restarted, and be exactly as untrained as it was the first
time. On the run of 2026-08-22 17:37--18:42 it noticed 184 412 setups and formed
no opinion at all.

It also closes the second half of that: **nothing wrote the training count
anywhere**, so the trade board could only say `NOT MEASURED` about how far the bot
was from its first decision (Rule 8). A checkpoint is a file with a number in it,
and a board that reads the file is reporting a measurement.

## What a checkpoint is

One JSON document per (part, component), replaced atomically. JSON rather than a
pickle because a model's coefficients are the one thing about this system a person
must be able to read and argue with, and rather than `numpy.memmap` because the
shape is a sparse dict of named features that grows as features appear, not a
fixed-width array.

Small by construction: the bull model carries one coefficient and four moment
figures per feature name. Written every
`learned_state_checkpoint_interval` training observations rather than every tick,
because the cost of a crash is that many observations and the cost of writing on
every tick is an fsync per market frame.

## What is refused, and why refusing is the honest answer

A checkpoint is restored only when the settings that give its numbers their meaning
are unchanged. `feature_half_life_observations` is the decay baked into every
stored moment; `minimum_feature_observations` decides which of them may be used at
all. Restoring coefficients standardised under one half-life into a model running
another is not a small error -- it is a model whose inputs are scaled by a rule
that no longer applies, reporting itself as trained.

So a mismatch starts the component cold and **says so**, in the standing and on the
board. Learning lost to a deliberate settings change is a cost the operator chose;
learning silently reinterpreted is a model nobody can trust afterwards.

A document from a different `SCHEMA_VERSION` is refused for the same reason.
"""

from __future__ import annotations

from __future__ import annotations

from runtime.durable_state import (  # noqa: F401 - this module is their public face
    RESTORED,
    SCHEMA_VERSION,
    STARTED_COLD_NO_CHECKPOINT,
    STARTED_COLD_SCHEMA_CHANGED,
    STARTED_COLD_SETTINGS_CHANGED,
    STARTED_COLD_UNREADABLE,
    CheckpointRefused,
    CheckpointSchedule,
    DurableStateStore,
    Restoration,
    compare_settings,
)

# One mechanism, two names, and this is the join. Learning parts say
# `LearnedStateStore` because that is what they keep in it.
LearnedStateStore = DurableStateStore
