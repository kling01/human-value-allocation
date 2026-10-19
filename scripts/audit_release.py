"""Conservative release-file audit. Findings show paths/lines, never secret values."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "provider_token": r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{25,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "credential_url": r"(?:postgres(?:ql)?|mysql|redis)://[^\s]+|https?://[^/\s:@]+:[^/\s@]+@",
    "local_user_path": r"/(?:Users|home)/[A-Za-z0-9_.-]+/",
    "internal_address": r"https?://(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)",
}
DENIED_PARTS = {".env", "runs", "private", "outputs", "node_modules", "__pycache__", ".venv", "business", "langfuse"}


def main():
    # Use the index when the standalone repository exists, otherwise a filtered
    # filesystem walk. Never inspect or print secrets from the parent workspace.
    if (ROOT / ".git").is_dir():
        paths = [ROOT / p for p in subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0") if p]
    else:
        paths = [p for p in ROOT.rglob("*") if p.is_file() and not any(
            part in {".venv", ".git", "__pycache__", ".pytest_cache", "build", "dist", "outputs"} or part.endswith(".egg-info")
            for part in p.relative_to(ROOT).parts)]
    findings = []
    for p in paths:
        rel = p.relative_to(ROOT)
        if p.is_symlink() or any(part in DENIED_PARTS for part in rel.parts):
            findings.append((str(rel), 0, "excluded path or symlink"))
        if p.suffix in {".png"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append((str(rel), 0, "unexpected binary"))
            continue
        # Tests intentionally contain non-routable credential-URL examples.
        if rel.as_posix() in {"scripts/audit_release.py", "tests/test_release.py"}:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS.items():
                if re.search(pattern, line):
                    findings.append((str(rel), lineno, name))
    for path, line, kind in findings:
        print(f"{path}:{line}: {kind}")
    print(f"Audited {len(paths)} release files; {len(findings)} findings.")
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
