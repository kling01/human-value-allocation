# How the analysis works

Version: 0.1.0a1. The analyzer produces evidence-linked observations of human contributions in a text conversation.

## Unit of analysis

One human message is evaluated with its preceding conversation and the assistant/tool messages before the next human message. This observed interval ends before the next human intervention. The original session and message IDs are retained; a JSON-serialized pair identifies the derived evaluation unit.

The input adapter constructs standard observations and calls `core.cleaner.run_pipeline`. The engine evaluates each cleaned unit using `core.pipeline.run_combined`, the versioned evaluator specification, and its shared LLM client. Full structured evidence is passed through `Metadata`, without truncating source content.

## Output interpretation

The underlying evaluator uses the engine's declared binary contract: `score=1` when the predicate “this message demonstrates at least one specific task contribution” is satisfied; `score=0` otherwise. This flag records contribution presence. The report presents the contribution categories, their evidence, and subsequent behavior. Failed evaluations have an unresolved status and are excluded from contribution counts.

`analysis.contributions` contains up to six observations, one per category. Each includes a description and a verbatim quote from the target human message. Contribution evidence must come from that message; earlier history and assistant messages provide context.

`analysis.followup` describes visible subsequent behavior. `observed` requires a quote from the subsequent assistant/tool interval. `not_observed`, `unclear`, and `no_followup` have distinct meanings. Review the quoted behavior to assess how the assistant used the human input and whether the response addressed the task.

Programmatic validation checks structure, categories, source IDs and exact quote containment. Human review checks whether the quoted evidence supports the interpretation, identifies missed contributions, and examines language and prompt effects.

Counts describe category occurrences across resolved turns. A turn can contribute to several categories, so read each count independently rather than as a percentage of a shared total. Unresolved turns are shown separately and excluded from these counts.

## Execution and privacy

Each turn receives the full applicable prefix. For long dialogues, total input usage can grow quadratically with the number of turns. The preview rejects inputs above its explicit limits instead of silently truncating them. Each request has at most one retry; at most 20 requests run concurrently. JSON-object output and Chat Completions compatibility are required. Provider-specific capabilities vary.

The application records usage, requested/actual model names, attempts and execution status. Missing token usage stays null. It does not write request bodies, raw provider errors, authentication headers or endpoint URLs into the shareable report. The source conversation and model explanations are included in the report. Review those before sharing. There is no automatic PII redaction.

The engine's cleaner standardizes structure. Review and redact sensitive content before sending a conversation to a provider or sharing a report. The application's public release was prepared separately through an allowlist export and audit; see [engine provenance](../ENGINE_PROVENANCE.md).

## Report provenance

The offline demo uses a synthetic conversation with authored annotations to demonstrate the interface and schema. Its provenance is `illustrative_fixture_not_model_output`. Live analysis is labeled `model_analysis` and records the model and evaluator identity alongside the conversation-specific observations.

## Future research

The next stage connects contribution analysis to comparison protocols, task-quality measures, and uncertainty analysis. Checkpoint comparisons measure changes between joint states that include human and agent work. Estimating the causal effect of a particular instruction requires a design that isolates that intervention. Resource accounting should distinguish remaining token usage from whole-session costs, and aggregation across turns should account for interactions between contributions.
