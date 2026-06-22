#!/usr/bin/env python3
"""Batch runner — score all ODH component repos for disconnected readiness.

Reads .github/config/repositories.yaml, clones each repo, runs the scorer
in-process, and produces per-repo JSON reports plus an aggregate summary.

Performance optimizations over subprocess-per-repo approach:
- Operator manifest parsed once, reused across all repos
- Operator arch-analyzer data computed once, reused across all repos
- arch-analyzer pre-run on all repos in parallel before scoring
- Dual output (JSON + markdown) in a single scorer run per repo
"""

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

GITHUB_URL = "https://github.com"

ARCH_ANALYZER_BIN = str(Path(__file__).parent / "bin" / "arch-analyzer")


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


def clone_operator(dest):
    url = f"{GITHUB_URL}/opendatahub-io/opendatahub-operator.git"
    cmd = [
        "git", "clone", "--depth", "1", "--single-branch", "--no-tags",
        url, str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Failed to clone operator: {result.stderr.strip()}")


def _run_arch_analyzer_on_dir(target_dir):
    """Run arch-analyzer binary on a single directory. Returns (dir, ok, error)."""
    json_path = Path(target_dir) / "component-architecture.json"
    if json_path.exists():
        json_path.unlink()

    try:
        subprocess.run(
            [ARCH_ANALYZER_BIN, "extract", ".", "--extractors", "docker,kustomize"],
            cwd=str(target_dir),
            capture_output=True, timeout=300, check=True,
        )
        return str(target_dir), True, ""
    except subprocess.CalledProcessError as e:
        stderr_msg = e.stderr.decode(errors='replace') if e.stderr else str(e)
        return str(target_dir), False, stderr_msg
    except (subprocess.TimeoutExpired, OSError) as e:
        return str(target_dir), False, str(e)


def run_arch_analyzer_batch(dirs):
    """Run arch-analyzer on multiple directories in parallel."""
    if not Path(ARCH_ANALYZER_BIN).is_file():
        raise SystemExit(
            f"ERROR: arch-analyzer binary not found at {ARCH_ANALYZER_BIN}.\n"
            f"       Install with: make install-arch-analyzer"
        )

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(dirs), 8)) as pool:
        futures = {pool.submit(_run_arch_analyzer_on_dir, d): d for d in dirs}
        for future in as_completed(futures):
            target_dir, ok, error = future.result()
            results[target_dir] = (ok, error)
    return results


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
        "--verbose", "-v", action="store_true",
        help="Pass --verbose to main.py for detailed diagnostic output "
             "(includes per-step timing and files_checked in JSON).",
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

    # --- Phase 0: Ensure operator clone ---
    if args.operator_path:
        operator_path = args.operator_path
    else:
        op_dir = repos_dir / "opendatahub-operator"
        if (op_dir / ".git").is_dir():
            print("Using cached opendatahub-operator clone.", file=sys.stderr)
        else:
            if op_dir.exists():
                shutil.rmtree(op_dir)
            print("Cloning opendatahub-operator (shared)...", file=sys.stderr)
            clone_operator(str(op_dir))
        operator_path = str(op_dir)

    # --- Phase 1: Clone all repos ---
    repo_dirs = {}
    for i, entry in enumerate(repos, 1):
        org, repo = entry["org"], entry["repo"]
        repo_dir = repos_dir / repo
        if (repo_dir / ".git").is_dir():
            print(f"  [{i}/{total}] {org}/{repo} — cached", file=sys.stderr)
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            t0 = time.monotonic()
            ok, clone_err = clone_repo(org, repo, repo_dir)
            if not ok:
                print(f"  [{i}/{total}] {org}/{repo} — SKIP (clone failed: {clone_err})", file=sys.stderr)
                continue
            elapsed = time.monotonic() - t0
            print(f"  [{i}/{total}] {org}/{repo} — cloned ({elapsed:.1f}s)", file=sys.stderr)
        repo_dirs[repo] = str(repo_dir)
    print("", file=sys.stderr)

    # --- Phase 2: Run arch-analyzer in parallel (operator + all repos) ---
    print("Running arch-analyzer on all repos...", file=sys.stderr)
    t0 = time.monotonic()
    arch_dirs = [operator_path] + list(repo_dirs.values())
    arch_results = run_arch_analyzer_batch(arch_dirs)
    elapsed = time.monotonic() - t0
    print(f"  arch-analyzer complete ({elapsed:.1f}s)\n", file=sys.stderr)

    for d, (ok, error) in arch_results.items():
        if not ok:
            print(f"  WARNING: arch-analyzer failed on {d}: {error}", file=sys.stderr)

    # --- Phase 3: Pre-compute operator data (once) ---
    from main import _run, load_manifest, _run_arch_analyzer

    t0 = time.monotonic()
    manifest, manifest_env_vars = load_manifest(operator_path)
    operator_arch_data = _run_arch_analyzer(ARCH_ANALYZER_BIN, operator_path)
    elapsed = time.monotonic() - t0
    print(f"Operator data loaded ({elapsed:.1f}s)\n", file=sys.stderr)

    # --- Phase 4: Score repos (in-process, dual output) ---
    def process_repo(i, entry):
        org, repo = entry["org"], entry["repo"]
        if repo not in repo_dirs:
            return i, entry, f"[{i}/{total}] {org}/{repo}\n    SKIP — clone failed"

        repo_dir = repo_dirs[repo]
        json_path = str(output_dir / f"{repo}.json")
        md_path = str(output_dir / f"{repo}.md")

        scorer_args = SimpleNamespace(
            repo_root=repo_dir,
            rules="all",
            report="json,markdown",
            output=[json_path, md_path],
            operator_path=operator_path,
            config=args.config,
            repo_config=None,
            no_production_scope=False,
            verbose=args.verbose,
            arch_analyzer=ARCH_ANALYZER_BIN,
        )

        log_lines = [f"[{i}/{total}] {org}/{repo}"]
        log_capture = io.StringIO()
        t1 = time.monotonic()

        try:
            rc = _run(
                scorer_args, operator_path,
                manifest=manifest,
                manifest_env_vars=manifest_env_vars,
                operator_arch_data=operator_arch_data,
                log_stream=log_capture,
            )
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        except Exception as e:
            import traceback
            rc = 1
            log_capture.write(f"    ERROR ({type(e).__name__}): {e}\n")
            log_capture.write(traceback.format_exc())

        elapsed = time.monotonic() - t1

        log_text = log_capture.getvalue().strip()
        if log_text:
            for line in log_text.splitlines():
                log_lines.append(f"    {line}")

        status = "OK" if rc == 0 else "NOT READY"
        log_lines.append(f"    → {status} ({elapsed:.1f}s)")
        return i, entry, "\n".join(log_lines)

    if args.parallel > 1:
        print(f"Scoring {len(repo_dirs)} repos ({args.parallel} parallel)...\n", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [
                pool.submit(process_repo, i, entry)
                for i, entry in enumerate(repos, 1)
            ]
            results_unordered = [f.result() for f in as_completed(futures)]
        results_ordered = sorted(results_unordered, key=lambda r: r[0])
    else:
        results_ordered = [process_repo(i, e) for i, e in enumerate(repos, 1)]

    for _, _, log in results_ordered:
        print(log, file=sys.stderr)
        print("", file=sys.stderr)

    all_entries = [entry for _, entry, _ in results_ordered]

    summary = generate_summary(output_dir, all_entries)
    print((output_dir / "summary.md").read_text(), file=sys.stderr)
    print(f"Reports saved to {output_dir}/", file=sys.stderr)

    has_failures = any(r["score"] in ("NOT READY", "ERROR") for r in summary)
    return 1 if has_failures else 0


if __name__ == "__main__":
    sys.exit(main())
