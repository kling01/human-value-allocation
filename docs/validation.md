# Release validation — v0.1.0a1

Checked on 2026-10-19 using Python 3.12 on macOS arm64.

- 17 automated tests passed from the source checkout.
- Built a wheel and source distribution. Installed the wheel in a separate virtual environment outside the checkout; all 17 tests and the offline CLI example passed there. `pip check` reported no broken requirements.
- A live smoke run on the synthetic example completed four model requests and produced four structurally valid analyses with quotes verified against source messages. The provider-reported model was `deepseek-v4-pro-ga-260813`. The private gateway and credentials are not included in the release.
- The older default gateway returned service errors. Those runs were correctly marked unresolved. Check provider compatibility with a small sample before a larger run.
- Browser inspection confirmed the example report renders and quote links navigate to the corresponding message.
- Gitleaks v8.30.1 scanned the new repository history with zero findings. A separate known-secret comparison and release-file audit also found no matches. The original repository history was not copied.

These checks cover packaging, execution, and evidence matching. The live model's category choices differ from the authored demo. Human review and rubric calibration are the next steps for evaluating how well the descriptions capture the quoted contributions.

The installation check used Python 3.12; Python 3.10+ is the declared compatibility target.
