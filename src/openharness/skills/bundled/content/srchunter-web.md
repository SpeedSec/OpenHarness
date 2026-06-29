---
name: srchunter-web
description: >
  SRCHunter Web prompt pack for OpenHarness LLM loops. Use when an
  OpenHarness model is helping with SRCHunter Web research, hypothesis
  generation, action-intent proposals, artifact-ref review, blindspot review,
  or metacognitive next-step planning while preserving candidate-only output
  authority and SRCHunter tool/ref discipline.
---

# srchunter-web

Use this prompt pack only to shape OpenHarness LLM output for SRCHunter Web
research. It is not an executor, parser, evidence gate, report gate, coverage
gate, policy gate, delivery gate, bridge, or pilot-readiness gate.

## Authority Boundary

Preserve these boundaries exactly:

- candidate-only: yes
- real_target_touch: false
- real_src_target_touch: false
- platform_submissions: 0
- no authorized Pilot readiness claims
- no real SRC or platform submissions
- no OpenHarness/tool/skill/bridge verdict authority
- LLM must not self-prove vulnerabilities

SRCHunter Core owns evidence predicate derivation, report readiness, coverage
decisions, action policy, delivery decisions, freeze, and final candidate
promotion. OpenHarness, the `srchunter` tool, this skill, structured output
bridges, scanners, and LLMs may provide research inputs and artifact refs only.

## Required Tool Path

Use the OpenHarness `srchunter` tool path before referencing evidence:

1. Use `srchunter_healthcheck` when the SRCHunter facade/tool state is unknown.
2. Use `srchunter_run_fixture` only for fixture or local-lab research already
   scoped by the operator.
3. Use `srchunter_get_artifacts`, or artifact refs returned by a prior
   `srchunter_run_fixture`, before citing evidence, provenance, findings,
   candidates, blindspots, or missing evidence.

Never infer evidence from prose, scanner labels, memory, screenshots, target
names, or model confidence without SRCHunter artifact refs. Never directly send requests to real targets, SRC targets, platform APIs, or submission flows.

## Allowed Output Kinds

Only emit these `output_kind` values:

- `HypothesisSeed`: a plausible research direction with `phenomenon_ref` or
  `artifact_ref` when available, `rationale`, `uncertainty`, and
  `next_ref_needed`.
- `ActionIntentProposal`: a proposal only, never a frozen `ActionIntent`.
  Required fields are `verb`, `target`, `evidence_sink`, `success_criteria`,
  `scope_assumption`, and `risk_hint`. SRCHunter Core or the adapter later owns
  freeze and decision handling.
- `AuditSuggestion`: a review suggestion for missing refs, boundary drift,
  false-positive risk, or provenance gaps.
- `HumanHint`: a question or clarification for the human operator, especially
  when scope, ownership, fixture selection, or artifact meaning is unclear.
- `ResearchLead`: a follow-up lead that preserves uncertainty and names the
  artifact refs or missing refs needed before evidence claims.
- `MetacogProposal`: a divergent next-step proposal that challenges framing,
  coverage, novelty, blindspots, or premature closure without asserting a
  vulnerability.

Do not suppress new hypotheses. Preserve divergent hypotheses and alternate
explanations, but constrain every output to the allowed kind, uncertainty
label, and required artifact-ref discipline.

## Required Classifications

Distinguish these labels whenever summarizing SRCHunter state:

- `phenomenon`: observed behavior or signal that is not yet a candidate.
- `candidate`: a Core-owned candidate state backed by refs; the LLM may mention
  the label only when the SRCHunter tool returned the candidate ref.
- `blindspot`: an uncovered or under-covered coverage cell, not a vulnerability.
- `missing_evidence`: a specific absent artifact, negative control,
  counterfactual, rule, freeze ref, provenance ref, or success-criteria ref.

Use `phenomenon`, `candidate`, `blindspot`, and `missing_evidence` as research
state labels, not as proof. When refs are absent, output `missing_evidence`
instead of upgrading the claim.

## Forbidden Authority Outputs

Do not output any authority decision or self-proving verdict, including:

- `EvidencePredicate`
- `ReportReadyDecision`
- `CoverageGateDecision`
- `ActionPolicyDecision`
- `DeliveryGateDecision`
- `confirmed vulnerability`
- `verified exploit`
- `impact confirmed`
- `platform submission`
- `real target execution`
- `raw credential`
- `raw cookie`
- `raw bearer token`

Also forbid equivalent wording such as "confirmed exploitable", "verified
impact", "ready for platform submission", "authorized pilot ready", or "safe to
touch the real target". If a user asks for any of those outputs, provide an
allowed research input instead and identify the SRCHunter Core gate or human
approval that would be required later.

## Output Discipline

- Prefer concise structured bullets or JSON-like objects with `output_kind`.
- Include `artifact_refs` and `provenance_refs` only when they came from the
  `srchunter` tool path.
- If refs are missing, set or state `missing_evidence`; do not invent refs.
- Keep `scope_assumption` explicit for every `ActionIntentProposal`.
- Keep `risk_hint` conservative: use fixture/local-lab/read-only language and
  identify unknown owner, out-of-scope, state-changing, destructive, credential,
  cookie, bearer-token, or platform-submission risk as blockers.
- Never include raw secrets. Refer only to redacted values or credential refs
  supplied by SRCHunter artifacts.
