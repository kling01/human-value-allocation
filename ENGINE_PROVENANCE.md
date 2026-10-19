# Evaluation engine provenance

This repository packages an allowlisted subset of the author's agent-eval-v2 workspace as `human_value._engine`. It has a new Git history; the original workspace history is not imported.

The source snapshot is fingerprinted in [docs/engine-manifest.json](docs/engine-manifest.json). Each entry records the SHA-256 of the source file and the published copy. The manifest is an integrity record, not a claim of independent security review.

Included: cleaner, feature extraction and navigation, rule evaluation, evaluator-spec compilation, pipeline orchestration, LLM client/runtime, and reports required by those modules. The public application uses the LLM evaluator path and an empty generic code-rule configuration.

Packaging changes:

1. Internal imports are namespaced under `human_value._engine` to avoid shadowing another application's `core` or `llm` modules.
2. The LLM client's automatic private-YAML loading is replaced by empty credential/model defaults. The application supplies runtime options explicitly.
3. The diagnostic Markdown renderer accepts a structured `analysis` object by serializing it before display. Evaluation and vote logic are unchanged.
4. An old deployment-address example is replaced with localhost.
5. Generic empty rule/evaluator configs are provided; original configs are not copied.
6. Domain-specific feature families, private routing allowlists and their heuristics are removed. The retained generic feature extraction preserves full trace IDs. The public application does not depend on those domain features.

Excluded: business rules, private model configs, source datasets and traces, run outputs, repair feedback, company-specific integrations, database and deprecated trace-fetch adapters, services, deployment bundles, frontend code, third-party research checkouts, and the original Git history.

The public entrypoint is `hva`. Historical standalone engine CLIs are implementation details, not supported interfaces of this preview. Rule expressions, if used in a future extension, are executable trusted configuration, not a sandbox for untrusted rules. This release does not expose arbitrary rule uploads.
