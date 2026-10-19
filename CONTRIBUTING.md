# Contributing

The most useful early contributions are rubric critiques with concrete evidence, new synthetic examples, and fixes to source attribution or input handling.

Install with `python -m pip install -e '.[dev]'`, then run `python -m pytest -q` and `python scripts/audit_release.py`. Keep model-dependent claims separate from software tests. Do not replace the shared evaluation engine with an additional judge.

Never submit private conversations, credentials, organization-specific endpoints or unlicensed datasets. Use synthetic examples when possible. Label authored fixtures and actual model outputs distinctly. Preserve the full session and message IDs in analysis and tests.

For rubric changes, explain which existing judgment should change and why. Avoid adding a single “human worth” number or treating unobserved behavior as proven absence. Proposals for causal or token-value estimates need a measurement protocol and validation evidence.
