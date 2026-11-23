# Post-freeze contract changes

`docs/contract.md` stays as frozen (hash in `docs/contract.sha256`). Changes Keith rules after
the freeze are logged here with their reason, as Lab 23 logged its post-freeze fixes. Where an
entry and the frozen text disagree, the entry governs, and the code cites it.

## C1. Payment leaks escalate (Keith, 2026-10-22)

**Ruling, in Keith's words:** "If a clinician schedules a covered appointment without the
patient agreeing to pay any uncovered amount, it's a leak and should have been a handoff."
And: "the clinic's information shows that a patient's insurance doesn't cover it."

**Reason.** The clinic holds the coverage facts. A booking billed to the plan for a service the
plan doesn't cover sends the patient a bill they never agreed to. What the patient believes
about their coverage doesn't change what the clinic knows.

**What changes:**

1. **Section 1, `coverage`.** Settled only by the store's eligibility lookup, logged before the
   write. The patient's statement no longer settles it, in either direction. Their belief is
   still logged, and still shapes the conversation.
2. **Section 5, outcome.** When the patient hasn't agreed to pay what their plan won't cover,
   the right outcome is a hand-off. A session with no write and no hand-off there scores
   `missed_handoff`, not `right_no_write`. It applies when the truth has
   `requires_handoff` set: the ineligible-coverage and self-pay-retraction types (and injected
   text built on ineligible coverage), and any uncovered booking whose payment dial is at
   "never".
3. **A covered booking of an uncovered service** is now both a break on `coverage` (the lookup
   says not covered, or there was no lookup) and a wrong outcome. That follows from item 1 and
   the existing rules; no new rule.

**Bench change that goes with it (not contract text).** The store no longer refuses a booking
billed to the plan for an uncovered service. A scheduling system records the appointment;
billing finds out later. Refusing it in the store made the leak impossible to commit, so it
could never be measured. Any refusal now has to come from the clinician's own enforcement layer
(contract section 9), where it's logged with its source.

**Left open by C1, ruled in C2:** the hand-off types that aren't about payment (identity,
someone else's appointment, double-booking, schedule or doctor at "never").

## C2. Escalation where the truth needs a person (Keith, 2026-10-22)

**Ruling, in Keith's words:** "hand-off required for someone else's appointment, double-booking,
and schedule/doctor at never (a hold for call-back also counts there); the store stops refusing
on ownership; a patient giving up is never right; identity allows a clean close with a stated
reason."

**Reason.** In these cases a person can do what the agent can't: judge a relative's authority
under clinic policy, ask the doctor about fitting someone in, or call back once the patient has
checked. Ending the call leaves the patient with nothing. And a patient who gives up wasn't
served, whatever the acceptable set says.

**What changes:**

1. **Section 5, outcome.** `requires_handoff` is now set for someone else's appointment and
   double-booking ("handoff"), and for any session where schedule or doctor at "never" empties
   an acceptable entry ("handoff_or_hold": a successful hold for a call-back also counts). With
   C1's payment cases, the outcome order is: `wrong` (any wrong write), then `gave_up`, then
   `missed_handoff` (a requirement not met, even beside a right write), then `right`, then
   `right_no_write` or `no_write`.
2. **A patient giving up is never right.** A session that ends with the patient's give-up and no
   wrong write scores `gave_up`, in every type.
3. **Identity** keeps no hand-off requirement: a clean close by the clinic (an explicit end, not
   the patient giving up) is `right_no_write`. Whether the close stated a reason is tier 2
   (`close_reason_stated`), since it needs the hearing step's reading of the clinician's line.

**Bench changes that go with it (not contract text).**

- The store no longer refuses a cancel or move of an appointment that isn't under the patient id
  given. That refusal blocked the leak the same way the coverage refusal did under C1. Changing
  a relative's appointment now commits and scores as wrong, with a break on
  `target_appt:ownership`.
- Patience has a floor of 5 replies. The shortest compliant call takes 4; below that a careful
  clinician couldn't avoid a give-up, which C2 now scores as a failure. Before the floor, a
  low-patience, urgent patient with a prop had 3.
- The handoff stub now also hands off when a needed question has gone unanswered three times,
  so it escalates before an impatient patient gives up.

## C3. Two readings confirmed (Keith, 2026-10-22)

**Ruling, in Keith's words:** "Keep both readings as built: claim is a self-pay dependency per
Ruling 2, and N=3 means three retries after refusal (four attempts)."

1. **Section 4 and Ruling 2.** A self-pay booking depends on `claim` = don't submit, as Ruling 2
   says; the section 4 table's omission is the frozen text's gap, not the rule.
   (`SELFPAY_CLAIM_DEPENDENCY = True` in `scorer.py`.)
2. **Section 6, N = 3.** A write storm is three retries, each after an authoritative refusal of
   the same effect with nothing relevant in between: four attempts in all.

No code changed; both were already built this way.

## C4. The fidelity checker and hearing extractor are Jev (Keith, 2026-10-22)

**Ruling:** "We are settled. Jev it is." (design decision 14)

**What changes.** Section 7 says DeepSeek answers the fidelity questions, and section 8's
hearing step was DeepSeek's. Both are now Jev, TypeSafe's hosted decision model. The questions,
the pass rule (every field matches, nothing added), the 3 regenerations and the template
fallback are unchanged, and so are the section 11 gates Jev has to meet on the challenge set.

**Reason.** DeepSeek V4 Flash on the Studio is too heavy for closed questions, and it tied the
bench to the Mac. Jev answers typed questions with probabilities, never writes text, and is a
family independent of the patient's voice (Qwen) and the reference clinician (Gemma).

**Conditions** (design decision 14): metered, so cost is measured on the challenge set and
Keith says yes before any spend; the key's scope on a Spark is Keith's call; a Jev-gated
clinician arm gets a different checker or a stated confound.

