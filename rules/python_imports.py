#!/usr/bin/env python3
"""Validate Python dependencies are available from bundled/internal mirrors."""

import re
from pathlib import Path

try:
    from rules.common import Finding, RuleResult
except ImportError:
    from common import Finding, RuleResult

GIT_DEP_PATTERN = re.compile(r'git\+https?://[^\s]+')
PIP_INSTALL_PATTERN = re.compile(r'(?:pip|pip3)\s+install\s+([^\s]+)')
SUBPROCESS_PIP = re.compile(r'subprocess.*pip\s+install')

KNOWN_BUNDLED = {
    "numpy", "pandas", "scikit-learn", "scipy", "matplotlib",
    "torch", "tensorflow", "transformers", "datasets",
    "flask", "fastapi", "uvicorn", "gunicorn",
    "requests", "urllib3", "certifi", "charset-normalizer",
    "pyyaml", "toml", "click", "typing-extensions",
    "boto3", "botocore", "s3transfer",
    "kfp", "kfp-server-api", "kfp-pipeline-spec",
    "kubernetes", "openshift-client",
    "pytest", "tox", "flake8", "black", "mypy",
}

SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__", ".tox", "venv", ".venv"}


def load_known_mirrors(config_path: Path) -> set[str]:
    """Load additional known-bundled packages from config."""
    extras = set()
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                data = yaml.safe_load(f)
            for pkg in data.get("bundled_packages", []):
                extras.add(pkg.lower())
        except Exception:
            pass
    return extras


def check_requirements_file(filepath: Path, root: Path, known: set[str]) -> list[Finding]:
    findings = []
    try:
        lines = filepath.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return findings

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            if stripped.startswith("-e ") or stripped.startswith("--editable"):
                pass
            elif stripped.startswith("-"):
                continue

        git_match = GIT_DEP_PATTERN.search(stripped)
        if git_match:
            findings.append(Finding(
                severity="blocker",
                file=str(filepath.relative_to(root)),
                line=i,
                image="",
                message=f"git+https dependency '{git_match.group()}' requires internet at install time.",
            ))
            continue

        pkg_match = re.match(r'^([a-zA-Z0-9_\-]+)', stripped)
        if pkg_match:
            pkg_name = pkg_match.group(1).lower().replace("-", "_").replace(".", "_")
            normalized_known = {k.lower().replace("-", "_").replace(".", "_") for k in known}
            if pkg_name not in normalized_known and len(pkg_name) > 1:
                is_test_req = "test" in filepath.name.lower() or "dev" in filepath.name.lower()
                findings.append(Finding(
                    severity="warning" if is_test_req else "info",
                    file=str(filepath.relative_to(root)),
                    line=i,
                    image="",
                    message=f"Package '{pkg_match.group(1)}' not in known-bundled list. "
                            f"Verify availability in internal PyPI mirror.",
                ))

    return findings


def check_runtime_pip_installs(filepath: Path, root: Path) -> list[Finding]:
    """Check for pip install calls in Python source (not requirements files)."""
    findings = []
    try:
        lines = filepath.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return findings

    for i, line in enumerate(lines, 1):
        if SUBPROCESS_PIP.search(line) or PIP_INSTALL_PATTERN.search(line):
            findings.append(Finding(
                severity="blocker",
                file=str(filepath.relative_to(root)),
                line=i,
                image="",
                message="Runtime pip install detected — will fail without internet or internal mirror.",
            ))

    return findings


def run(repo_root: str) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule="python-imports-bundled")

    config_path = root / ".disconnected-readiness" / "known_mirrors.yaml"
    known = KNOWN_BUNDLED | load_known_mirrors(config_path)

    req_patterns = [
        "requirements*.txt", "constraints*.txt",
        "**/requirements*.txt", "**/constraints*.txt",
    ]
    for pattern in req_patterns:
        for filepath in root.glob(pattern):
            if any(d in filepath.parts for d in SKIP_DIRS):
                continue
            for finding in check_requirements_file(filepath, root, known):
                result.findings.append(finding)
                if finding.severity == "blocker":
                    result.passed = False

    for filepath in root.rglob("*.py"):
        if any(d in filepath.parts for d in SKIP_DIRS):
            continue
        for finding in check_runtime_pip_installs(filepath, root):
            result.findings.append(finding)
            if finding.severity == "blocker":
                result.passed = False

    setup_files = list(root.glob("setup.py")) + list(root.glob("**/setup.py"))
    pyproject_files = list(root.glob("pyproject.toml")) + list(root.glob("**/pyproject.toml"))
    for filepath in setup_files + pyproject_files:
        if any(d in filepath.parts for d in SKIP_DIRS):
            continue
        try:
            content = filepath.read_text()
            for match in GIT_DEP_PATTERN.finditer(content):
                result.passed = False
                result.findings.append(Finding(
                    severity="blocker",
                    file=str(filepath.relative_to(root)),
                    line=0,
                    image="",
                    message=f"git+https dependency '{match.group()}' in build config.",
                ))
        except (OSError, UnicodeDecodeError):
            continue

    return result


if __name__ == "__main__":
    import sys
    import json

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    r = run(repo)
    print(json.dumps({
        "rule": r.rule,
        "passed": r.passed,
        "findings": [
            {"severity": f.severity, "file": f.file, "line": f.line,
             "message": f.message}
            for f in r.findings
        ],
    }, indent=2))
