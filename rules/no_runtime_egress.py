#!/usr/bin/env python3
"""Detect outbound HTTP calls in runtime code that would fail disconnected."""

import re
from pathlib import Path

try:
    from rules.common import Finding, RuleResult, get_tracked_files
except ModuleNotFoundError:
    from common import Finding, RuleResult, get_tracked_files

EGRESS_PATTERNS = {
    ".go": [
        (re.compile(r'http\.(Get|Post|Head|Do|NewRequest)\s*\('), "http.{method} call"),
        (re.compile(r'net\.Dial\s*\('), "net.Dial call"),
        (re.compile(r'http\.DefaultClient'), "http.DefaultClient usage"),
    ],
    ".py": [
        (re.compile(r'requests\.(get|post|put|delete|head|patch)\s*\('), "requests.{method} call"),
        (re.compile(r'urllib\.request\.(urlopen|Request)\s*\('), "urllib.request call"),
        (re.compile(r'httpx\.(get|post|put|delete|AsyncClient)\s*\('), "httpx call"),
        (re.compile(r'aiohttp\.ClientSession\s*\('), "aiohttp session"),
        (re.compile(r'subprocess.*(?:curl|wget)'), "curl/wget via subprocess"),
    ],
    ".ts": [
        (re.compile(r'fetch\s*\('), "fetch() call"),
        (re.compile(r'axios\.(get|post|put|delete|request)\s*\('), "axios.{method} call"),
        (re.compile(r'http\.request\s*\('), "http.request call"),
    ],
    ".tsx": [
        (re.compile(r'fetch\s*\('), "fetch() call"),
        (re.compile(r'axios\.(get|post|put|delete|request)\s*\('), "axios.{method} call"),
    ],
    ".sh": [
        (re.compile(r'\bcurl\s+'), "curl invocation"),
        (re.compile(r'\bwget\s+'), "wget invocation"),
    ],
}

BUILD_DIRS = {"Dockerfile", "Makefile", "Containerfile"}
BUILD_PATHS = {".github", "ci", "hack", "build", "Dockerfile", "Makefile"}
SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__"}
TEST_DIRS = {"test", "tests", "testdata", "e2e"}
TEST_SUFFIXES = {"_test.go", "_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"}


def is_test_file(filepath: Path) -> bool:
    name = filepath.name
    if any(name.endswith(s) for s in TEST_SUFFIXES) or name.startswith("test_"):
        return True
    return any(d in filepath.parts for d in TEST_DIRS)


def is_build_context(filepath: Path) -> bool:
    return (
        filepath.name in BUILD_DIRS
        or any(d in filepath.parts for d in BUILD_PATHS)
    )


def has_configurable_url(line: str) -> bool:
    """Check if the URL in this line appears configurable (env var, config, etc)."""
    indicators = ["os.Getenv", "os.environ", "config.", "settings.", "env.",
                   "process.env", "viper.", "${", "getenv"]
    return any(ind in line for ind in indicators)


def run(repo_root: str) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule="no-runtime-egress")
    tracked = get_tracked_files(root)

    for filepath in root.rglob("*"):
        if tracked is not None and filepath.resolve() not in tracked:
            continue
        if any(d in filepath.parts for d in SKIP_DIRS):
            continue
        if is_build_context(filepath):
            continue

        suffix = filepath.suffix
        if suffix not in EGRESS_PATTERNS:
            continue

        try:
            lines = filepath.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        patterns = EGRESS_PATTERNS[suffix]
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("#"):
                continue

            for pattern, desc in patterns:
                match = pattern.search(line)
                if not match:
                    continue

                configurable = has_configurable_url(line)
                hardcoded_url = bool(re.search(r'https?://', line))
                in_test = is_test_file(filepath)

                if in_test:
                    severity = "info"
                    msg = f"{desc} — test file, informational only."
                elif hardcoded_url and not configurable:
                    severity = "blocker"
                    msg = f"{desc} with hardcoded external URL — will fail disconnected."
                elif configurable:
                    severity = "info"
                    msg = f"{desc} — URL appears configurable. Verify mirror support."
                else:
                    severity = "blocker"
                    msg = f"{desc} — endpoint may not be reachable in disconnected environments."

                if severity == "blocker":
                    result.passed = False

                result.findings.append(Finding(
                    severity=severity,
                    file=str(filepath.relative_to(root)),
                    line=i,
                    image="",
                    message=msg,
                ))

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
