# Method and release scope

Version: 0.1.0a1. Status: initial rubric, uncalibrated research preview.

## Unit of analysis

One human message is evaluated with its preceding conversation and the assistant/tool messages before the next human message. Later human interventions are excluded. This is an observed interval, not a counterfactual experiment. The original session and message IDs are retained; a JSON-serialized pair identifies the derived evaluation unit.

The input adapter constructs standard observations and calls `core.cleaner.run_pipeline`. The engine evaluates each cleaned unit using `core.pipeline.run_combined`, the versioned evaluator specification, and its shared LLM client. Full structured evidence is passed through `Metadata`, without truncating source content.

## Output interpretation

The underlying evaluator uses the engine's declared binary contract: `score=1` when the predicate “this message demonstrates at least one specific task contribution” is satisfied; `score=0` when it is not. The report does not show that flag as a human-value score. No three-state rule semantics are changed. A failed evaluation is never treated as either business outcome.

`analysis.contributions` contains up to six observations, one per category. Each includes a description and a verbatim quote from the target human message. A quote from history or from an assistant cannot support an attribution to the target human turn.

`analysis.followup` describes visible subsequent behavior. `observed` requires a quote from the subsequent assistant/tool interval. `not_observed`, `unclear`, and `no_followup` have distinct meanings. Observed uptake does not establish correctness, causation, necessity or saved computation.

Programmatic validation checks structure, categories, source IDs and exact quote containment. It does not prove that a quote semantically supports the model's interpretation. False positives, false negatives, language bias and prompt sensitivity remain possible. No empirical reliability or accuracy claim accompanies this release.

Counts describe category occurrences across resolved turns. They overlap. Unresolved turns are shown explicitly, and counts exclude them. Counts are not additive values or percentages of a shared total.

## Execution and privacy

Each turn receives the full applicable prefix. For long dialogues, total input usage can grow quadratically with the number of turns. The preview rejects inputs above its explicit limits instead of silently truncating them. Each request has at most one retry; at most 20 requests run concurrently. JSON-object output and Chat Completions compatibility are required. Provider-specific capabilities vary.

The application records usage, requested/actual model names, attempts and execution status. Missing token usage stays null. It does not write request bodies, raw provider errors, authentication headers or endpoint URLs into the shareable report. The source conversation and model explanations are included in the report. Review those before sharing. There is no automatic PII redaction.

The engine's cleaner standardizes structure; it is not a secret-removal tool. The application's public release was prepared separately through an allowlist export and audit.

## Demonstration versus evidence

The offline demo contains authored annotations of a synthetic conversation. It demonstrates the interface and schema; it is neither an accuracy benchmark nor live inference. Live analysis is labeled `model_analysis`. Any published live example remains a single illustrative case, not a performance measurement.

## Future research

Estimating a human intervention's marginal impact requires an explicit comparison protocol, task-quality measurement, and uncertainty analysis. Comparing checkpoints measures the difference between joint states, which can include both human and agent work. It does not by itself isolate the causal effect of a human instruction. Reduction in remaining tokens also differs from net whole-session token savings. Per-turn effects need an interaction-aware allocation protocol before they can be summed across a conversation.

These are future capabilities. No trained attribution or token-value model is included in v0.
