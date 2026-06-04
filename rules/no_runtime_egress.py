#!/usr/bin/env python3
"""Detect outbound HTTP calls in runtime code that would fail disconnected."""

import re
from pathlib import Path

try:
    from rules.common import (
        Finding, RuleResult, get_tracked_files, is_in_production_scope,
        SKIP_DIRS,
    )
except ModuleNotFoundError:
    from common import (
        Finding, RuleResult, get_tracked_files, is_in_production_scope,
        SKIP_DIRS,
    )

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

INTERNAL_URL_PATTERNS = [
    "kubernetes.default.svc",
    ".svc.cluster.local",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
]


def has_configurable_url(line: str) -> bool:
    """Check if the URL in this line appears configurable (env var, config, etc)."""
    indicators = ["os.Getenv", "os.environ", "config.", "settings.", "env.",
                   "process.env", "viper.", "${", "getenv"]
    return any(ind in line for ind in indicators)


def run(repo_root: str, production_scope=None) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule="no-runtime-egress")
    tracked = get_tracked_files(root)

    for filepath in root.rglob("*"):
        if tracked is not None and filepath.resolve() not in tracked:
            continue
        if any(d in filepath.parts for d in SKIP_DIRS):
            continue

        suffix = filepath.suffix
        if suffix not in EGRESS_PATTERNS:
            continue

        try:
            lines = filepath.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        in_prod = is_in_production_scope(filepath, production_scope)
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

                internal_url = hardcoded_url and any(
                    p in line for p in INTERNAL_URL_PATTERNS
                )

                if hardcoded_url and not configurable and not internal_url:
                    severity = "blocker"
                    msg = f"{desc} with hardcoded external URL — will fail disconnected."
                elif internal_url:
                    severity = "info"
                    msg = f"{desc} — cluster-internal URL, reachable in disconnected environments."
                elif configurable:
                    severity = "info"
                    msg = f"{desc} — URL appears configurable. Verify mirror support."
                elif not hardcoded_url:
                    severity = "info"
                    msg = f"{desc} — no hardcoded URL, likely internal/relative API call."
                else:
                    severity = "blocker"
                    msg = f"{desc} — endpoint may not be reachable in disconnected environments."

                if in_prod is False and severity in ("blocker", "warning"):
                    severity = "info"
                    msg += " [out of production scope]"

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
