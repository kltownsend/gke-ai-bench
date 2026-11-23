# clinicbench

**Where does a clinical scheduling agent book an appointment before the patient has made clear
whether they'll pay out of pocket, which times work, or which doctor they want?**

clinicbench puts a scheduling agent in conversation with a simulated patient and finds the
point where it stops asking and commits. The patient's truth (what they'll pay, when they can
come, who they'll see) is set in code before the conversation starts. The agent has to draw it
out. A validator compares what the agent wrote to the clinic's schedule against that truth.
No model grades the outcome.

Status: design. Nothing here runs yet. The design is in [docs/design.md](docs/design.md).

## How it works

- **Patients are patients.** A model voices each patient freely, in their own temperament, but
  code owns what the patient wants, what they'll accept, and how clearly they reveal it.
- **Clinicians are held to scripted speech.** Coverage questions, read-backs, consent questions
  and cost notices are checked against a small declared set of legal duties and a labeled
  practice standard.
- **Ambiguity is the dial.** Payment, schedule and doctor can each be revealed clearly, hedged,
  contradicted or withheld, and can take one question or several to pin down. Each dial is
  varied alone first, then combined.
- **Any enforcement method plugs in** through an adapter: a deterministic boundary on the agent's
  tool calls, code running the workflow, a third-model validator, a tuned model, or none.

## What it scores

- **Outcome:** did the booking fall inside the patient's acceptable set?
- **Commitment under ambiguity:** did the agent book while a dimension it depended on was still
  unresolved, even if it guessed right?
- **Storms:** did it repeat actions without learning anything new in between?
- **Process:** legal duties and the practice standard, reported separately and never merged.

## What this does not claim

A simulated patient in a synthetic clinic isn't a real one. The checker that keeps the patient's
voice faithful to its state is a model, so results are bounded by how well a model can judge
that, and no human baseline exists yet. The compliance set is federal plus one state, not legal
compliance for scheduling calls in general.

## Lineage and the open-bench boundary

Built from the Layer2C Labs clinic bench (Labs 23, 024 and 025). This repo publishes the
instrument: the simulator, the scorer and the scenario generator. It doesn't publish the
judgment that turns a score into a placement or a verdict.

MIT licensed.
