#!/usr/bin/env python3
"""Batch runner — score all ODH component repos for disconnected readiness.

Reads .github/config/repositories.yaml, clones each repo, runs main.py,
and produces per-repo JSON reports plus an aggregate summary.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

GITHUB_URL = "https://github.com"


def load_repos(config_path):
    path = Path(config_path)
    if not path.exists():
        raise SystemExit(f"Config not found: {config_path}")
    text = path.read_text()
    try:
        import yaml
        data = yaml.safe_load(text)
    except ImportError:
        data = _parse_repos_fallback(text)

    entries = data.get("included_repositories") or []
    repos = []
    for entry in entries:
        if "/" not in entry:
            print(f"WARNING: skipping invalid entry '{entry}' (expected org/repo)", file=sys.stderr)
            continue
        org, repo = entry.split("/", 1)
        repos.append({"org": org, "repo": repo})
    return repos


def _parse_repos_fallback(text):
    """Minimal YAML parser for included_repositories list — no PyYAML needed."""
    entries = []
    in_list = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "included_repositories:":
            in_list = True
            continue
        if in_list and stripped.startswith("- "):
            entries.append(stripped[2:].strip().strip('"').strip("'"))
        elif in_list and not stripped.startswith("- "):
            break
    return {"included_repositories": entries}


def clone_repo(org, repo, dest):
    url = f"{GITHUB_URL}/{org}/{repo}.git"
    cmd = [
        "git", "clone", "--depth", "1", "--single-branch", "--no-tags",
        url, str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, f"git clone failed: {result.stderr.strip()}"
    return True, ""


SCORER_TIMEOUT = 300


def run_scorer(repo_path, operator_path, output_path, report_format="json",
               exceptions=None, timing=False):
    script = Path(__file__).parent / "main.py"
    cmd = [
        sys.executable, str(script),
        str(repo_path),
        "--report", report_format,
        "--operator-path", str(operator_path),
        "-o", str(output_path),
    ]
    if exceptions:
        cmd += ["--config", exceptions]
    if timing:
        cmd += ["--timing"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SCORER_TIMEOUT,
        )
        return result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        return 1, f"TIMEOUT after {SCORER_TIMEOUT}s"


def clone_operator(dest):
    url = f"{GITHUB_URL}/opendatahub-io/opendatahub-operator.git"
    cmd = [
        "git", "clone", "--depth", "1", "--single-branch", "--no-tags",
        url, str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Failed to clone operator: {result.stderr.strip()}")


def generate_summary(output_dir, results):
    summary_data = []
    for entry in results:
        report_path = output_dir / f"{entry['repo']}.json"
        full_repo = f"{entry['org']}/{entry['repo']}"
        record = {
            "repo": full_repo,
            "score": "ERROR",
            "blockers": 0,
            "infos": 0,
            "rules": [],
        }
        if report_path.exists():
            try:
                data = json.loads(report_path.read_text())
                record["score"] = data.get("score", "ERROR")
                record["rules"] = data.get("rules", [])
                for rule in record["rules"]:
                    record["blockers"] += rule.get("blockers", 0)
                    record["infos"] += rule.get("infos", 0)
            except (json.JSONDecodeError, KeyError):
                pass
        summary_data.append(record)

    (output_dir / "summary.json").write_text(json.dumps(summary_data, indent=2) + "\n")

    lines = [
        f"# Disconnected Readiness — All Repos ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
        "",
        "| Repository | Score | Blockers | Infos |",
        "|-----------|-------|----------|-------|",
    ]
    for r in summary_data:
        heading = f"{r['repo']} — {r['score']}"
        anchor = re.sub(r'\s+', '-', heading.lower())
        anchor = re.sub(r'[^a-z0-9_-]', '', anchor)
        repo_link = f"[{r['repo']}]({GITHUB_URL}/{r['repo']})"
        lines.append(
            f"| {repo_link} | [{r['score']}](#{anchor}) | {r['blockers']} | {r['infos']} |"
        )

    totals = {
        "ready": sum(1 for r in summary_data if r["score"] == "READY"),
        "info": sum(1 for r in summary_data if r["score"] == "INFO"),
        "not_ready": sum(1 for r in summary_data if r["score"] == "NOT READY"),
        "error": sum(1 for r in summary_data if r["score"] == "ERROR"),
    }
    lines += [
        "",
        f"**Totals:** {totals['ready']} READY | {totals['info']} INFO | "
        f"{totals['not_ready']} NOT READY | {totals['error']} ERROR",
    ]

    for r in summary_data:
        if not r["rules"]:
            continue
        lines += ["", f"---", "", f"## {r['repo']} — {r['score']}"]
        for rule in r["rules"]:
            findings = [f for f in rule.get("findings", []) if f["severity"] != "info"]
            if not findings:
                lines.append(f"- **{rule['name']}**: PASS")
                continue
            lines.append(f"- **{rule['name']}**: {rule['blockers']} blocker(s), {rule.get('infos', 0)} info(s)")
            for f in findings:
                if f.get("file") and f.get("line"):
                    loc = f"{f['file']}:{f['line']}"
                elif f.get("file"):
                    loc = f['file']
                else:
                    loc = ""
                lines.append(f"  - [{f['severity']}] {loc} {f['message']}")

    (output_dir / "summary.md").write_text("\n".join(lines) + "\n")
    return summary_data


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run disconnected-readiness checks on all ODH component repos.",
    )
    parser.add_argument(
        "--output-dir", default="reports",
        help="Base output directory (default: reports/). "
             "Reports go into a YYYY-MM-DD subdirectory.",
    )
    parser.add_argument(
        "--repos-config",
        default=str(Path(__file__).parent / ".github" / "config" / "repositories.yaml"),
        help="Path to repositories config YAML (default: .github/config/repositories.yaml).",
    )
    parser.add_argument(
        "--config",
        help="Path to central config.yaml (passed through to main.py).",
    )
    parser.add_argument(
        "--operator-path",
        help="Path to pre-cloned opendatahub-operator. "
             "If omitted, clones to a temporary directory.",
    )
    parser.add_argument(
        "--repo", dest="single_repo",
        help="Run only this repo (e.g. 'odh-dashboard' or 'opendatahub-io/odh-dashboard').",
    )
    parser.add_argument(
        "--parallel", type=int, default=4, metavar="N",
        help="Run N repos concurrently (default: 4). Use 1 for sequential.",
    )
    parser.add_argument(
        "--timing", action="store_true",
        help="Pass --timing to main.py and show per-repo wall time.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    repos = load_repos(args.repos_config)
    if not repos:
        raise SystemExit("No repos found in config.")

    if args.single_repo:
        needle = args.single_repo
        repos = [r for r in repos if r["repo"] == needle or f"{r['org']}/{r['repo']}" == needle]
        if not repos:
            raise SystemExit(f"Repo '{needle}' not found in config.")

    run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    output_dir = Path(args.output_dir) / run_timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    total = len(repos)
    print(f"Scanning {total} repos → {output_dir}/\n", file=sys.stderr)

    repos_dir = Path(__file__).parent / ".repos"
    repos_dir.mkdir(exist_ok=True)

    def process_repo(i, entry, operator_path):
        org = entry["org"]
        repo = entry["repo"]
        json_path = output_dir / f"{repo}.json"
        log = []

        log.append(f"[{i}/{total}] {org}/{repo}")

        repo_dir = repos_dir / repo
        if (repo_dir / ".git").is_dir():
            log.append(f"    Using cached clone at {repo_dir}")
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            t0 = time.monotonic()
            ok, clone_err = clone_repo(org, repo, repo_dir)
            if not ok:
                log.append(f"    SKIP — clone failed: {clone_err}")
                return i, entry, "\n".join(log)
            if args.timing:
                log.append(f"    [timing] clone: {time.monotonic() - t0:.1f}s")

        t1 = time.monotonic()
        rc, scorer_log = run_scorer(
            repo_dir, operator_path, json_path, "json",
            args.config, timing=args.timing,
        )
        elapsed = time.monotonic() - t1

        md_path = output_dir / f"{repo}.md"
        run_scorer(repo_dir, operator_path, md_path, "markdown",
                   args.config, timing=False)

        if scorer_log.strip():
            for line in scorer_log.strip().splitlines():
                log.append(f"    {line}")
        status = "OK" if rc == 0 else "NOT READY"
        log.append(f"    → {status} ({elapsed:.1f}s)")
        return i, entry, "\n".join(log)

    def run_with_operator(operator_path):
        if args.parallel > 1:
            print(f"Running {args.parallel} repos in parallel...\n", file=sys.stderr)
            with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                futures = [
                    pool.submit(process_repo, i, entry, operator_path)
                    for i, entry in enumerate(repos, 1)
                ]
                results_unordered = [f.result() for f in as_completed(futures)]
            results_ordered = sorted(results_unordered, key=lambda r: r[0])
        else:
            results_ordered = [process_repo(i, e, operator_path) for i, e in enumerate(repos, 1)]

        for _, _, log in results_ordered:
            print(log, file=sys.stderr)
            print("", file=sys.stderr)

        return [entry for _, entry, _ in results_ordered]

    if args.operator_path:
        results = run_with_operator(args.operator_path)
    else:
        op_dir = repos_dir / "opendatahub-operator"
        if (op_dir / ".git").is_dir():
            print("Using cached opendatahub-operator clone.", file=sys.stderr)
        else:
            if op_dir.exists():
                shutil.rmtree(op_dir)
            print("Cloning opendatahub-operator (shared)...", file=sys.stderr)
            clone_operator(str(op_dir))
        print("", file=sys.stderr)
        results = run_with_operator(str(op_dir))

    summary = generate_summary(output_dir, results)
    print((output_dir / "summary.md").read_text(), file=sys.stderr)
    print(f"Reports saved to {output_dir}/", file=sys.stderr)

    has_failures = any(r["score"] in ("NOT READY", "ERROR") for r in summary)
    return 1 if has_failures else 0


if __name__ == "__main__":
    sys.exit(main())
