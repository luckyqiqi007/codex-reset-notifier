"""Offline guard for accidental secret-bearing files; never prints match values."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"webhook/send\?key=[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
)


def main():
    try:
        raw = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
        files = [ROOT / name for name in raw.decode().split("\0") if name]
    except subprocess.CalledProcessError:
        print("Run from a Git checkout so ignored local credentials are excluded.", file=sys.stderr)
        return 1
    issues = []
    for path in files:
        rel = path.relative_to(ROOT)
        if path.is_symlink():
            issues.append((str(rel), "symlink_requires_review"))
            continue
        if not path.is_file():
            continue
        if ((path.name.startswith("config.") and path.suffix == ".toml" and path.name != "config.example.toml")
                or path.name == ".env" or path.name.startswith(".env.")
                or "secrets" in rel.parts or "data" in rel.parts
                or path.suffix in (".db", ".sqlite", ".pem", ".key", ".log")):
            issues.append((str(rel), "private_runtime_file"))
        text = path.read_text(errors="replace")
        if any(pattern.search(text) for pattern in PATTERNS):
            issues.append((str(rel), "possible_credential"))
    for path, reason in issues:
        print(f"FAIL {path}: {reason}")
    print(f"Checked {len(files)} public files; findings={len(issues)}")
    return bool(issues)


if __name__ == "__main__":
    raise SystemExit(main())
