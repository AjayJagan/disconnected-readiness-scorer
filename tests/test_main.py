"""Tests for main.py orchestrator functions."""

import json
import sys
from dataclasses import dataclass, field
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from main import (
    RULE_REGISTRY,
    _render_template_simple,
    adapt_manifest_result,
    compute_score,
    parse_args,
    print_summary,
    render_json,
    render_markdown,
    resolve_rules,
    main,
)
from rules.common import Finding, RuleResult


# --- parse_args ---

class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.repo_root == "."
        assert args.rules == "all"
        assert args.report == "markdown"
        assert args.operator_path is None
        assert args.output is None

    def test_positional_repo(self):
        args = parse_args(["/tmp/repo"])
        assert args.repo_root == "/tmp/repo"

    def test_rules_flag(self):
        args = parse_args([".", "--rules", "csv,tags"])
        assert args.rules == "csv,tags"

    def test_report_json(self):
        args = parse_args([".", "--report", "json"])
        assert args.report == "json"

    def test_operator_path(self):
        args = parse_args([".", "--operator-path", "/tmp/op"])
        assert args.operator_path == "/tmp/op"

    def test_output_short(self):
        args = parse_args([".", "-o", "out.md"])
        assert args.output == "out.md"


# --- resolve_rules ---

class TestResolveRules:
    def test_all_returns_defaults(self):
        result = resolve_rules("all")
        assert result == ["csv", "tags", "egress", "python"]

    def test_specific_rules(self):
        assert resolve_rules("csv,tags") == ["csv", "tags"]

    def test_single_rule(self):
        assert resolve_rules("egress") == ["egress"]

    def test_unknown_rule_exits(self):
        with pytest.raises(SystemExit, match="Unknown rule 'nope'"):
            resolve_rules("nope")

    def test_whitespace_stripped(self):
        assert resolve_rules(" csv , tags ") == ["csv", "tags"]


# --- compute_score ---

class TestComputeScore:
    def test_ready(self):
        results = [RuleResult(rule="a"), RuleResult(rule="b")]
        assert compute_score(results) == "READY"

    def test_ready_with_info(self):
        r = RuleResult(rule="a", findings=[Finding("info", "", 0, "", "ok")])
        assert compute_score([r]) == "READY"

    def test_warning(self):
        r = RuleResult(rule="a", findings=[Finding("warning", "f", 1, "", "w")])
        assert compute_score([r]) == "WARNING"

    def test_not_ready(self):
        r = RuleResult(rule="a", passed=False,
                       findings=[Finding("blocker", "f", 1, "img", "bad")])
        assert compute_score([r]) == "NOT READY"

    def test_not_ready_overrides_warning(self):
        r1 = RuleResult(rule="a", findings=[Finding("warning", "", 0, "", "w")])
        r2 = RuleResult(rule="b", passed=False)
        assert compute_score([r1, r2]) == "NOT READY"

    def test_empty_results(self):
        assert compute_score([]) == "READY"


# --- adapt_manifest_result ---

@dataclass
class FakeImageEntry:
    env_var: str
    image: str = ""

@dataclass
class FakeManifest:
    images: list = field(default_factory=list)
    components: list = field(default_factory=list)
    known_issues: list = field(default_factory=list)


class TestAdaptManifestResult:
    def test_basic(self):
        manifest = FakeManifest(
            images=[FakeImageEntry("VAR_A"), FakeImageEntry("VAR_B")],
            components=["comp1"],
        )
        result = adapt_manifest_result(manifest)
        assert result.rule == "operator-manifest"
        assert result.passed is True
        assert len(result.findings) == 1
        assert "2 RELATED_IMAGE vars" in result.findings[0].message

    def test_duplicate_env_vars_counted_unique(self):
        manifest = FakeManifest(
            images=[FakeImageEntry("VAR_A"), FakeImageEntry("VAR_A")],
            components=[],
        )
        result = adapt_manifest_result(manifest)
        assert "1 RELATED_IMAGE vars" in result.findings[0].message

    def test_known_issues_become_warnings(self):
        manifest = FakeManifest(
            images=[],
            components=[],
            known_issues=["stale ref", "missing var"],
        )
        result = adapt_manifest_result(manifest)
        assert len(result.findings) == 3  # 1 info + 2 warnings
        warnings = [f for f in result.findings if f.severity == "warning"]
        assert len(warnings) == 2
        assert "stale ref" in warnings[0].message


# --- print_summary ---

class TestPrintSummary:
    def test_output_to_stderr(self, capsys):
        results = [
            RuleResult(rule="r1", passed=True, findings=[
                Finding("info", "", 0, "", "ok"),
            ]),
            RuleResult(rule="r2", passed=False, findings=[
                Finding("blocker", "f.go", 1, "img", "bad"),
            ]),
        ]
        print_summary("NOT READY", results)
        err = capsys.readouterr().err
        assert "NOT READY" in err
        assert "PASS" in err
        assert "BLOCKER" in err

    def test_warning_tag(self, capsys):
        results = [
            RuleResult(rule="r1", findings=[
                Finding("warning", "x.py", 1, "", "needs review"),
            ]),
        ]
        print_summary("WARNING", results)
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "1 warning(s)" in err


# --- render_json ---

class TestRenderJson:
    def test_structure(self):
        results = [
            RuleResult(rule="a", passed=True, findings=[
                Finding("blocker", "f.go", 10, "img", "msg"),
                Finding("warning", "g.go", 20, "", "wmsg"),
            ]),
        ]
        raw = render_json("NOT READY", results, "my-repo")
        data = json.loads(raw)
        assert data["repo"] == "my-repo"
        assert data["score"] == "NOT READY"
        assert len(data["rules"]) == 1
        rule = data["rules"][0]
        assert rule["name"] == "a"
        assert rule["passed"] is True
        assert rule["blockers"] == 1
        assert rule["warnings"] == 1
        assert len(rule["findings"]) == 2

    def test_empty_results(self):
        data = json.loads(render_json("READY", [], "repo"))
        assert data["rules"] == []
        assert data["score"] == "READY"


# --- _render_template_simple ---

class TestRenderTemplateSimple:
    def test_variable_substitution(self):
        result = _render_template_simple("Hello {{ name }}", {"name": "world"})
        assert result == "Hello world"

    def test_upper_filter(self):
        result = _render_template_simple("{{ x | upper }}", {"x": "abc"})
        assert result == "ABC"

    def test_for_loop(self):
        template = "{% for item in items %}[{{ item.v }}]{% endfor %}"
        ctx = {"items": [{"v": "a"}, {"v": "b"}]}
        assert _render_template_simple(template, ctx) == "[a][b]"

    def test_dot_access_in_loop(self):
        template = "{% for r in rules %}{{ r.name }},{% endfor %}"
        ctx = {"rules": [{"name": "x"}, {"name": "y"}]}
        assert _render_template_simple(template, ctx) == "x,y,"

    def test_nested_for_raises(self):
        template = "{% for a in x %}{% for b in y %}{% endfor %}{% endfor %}"
        with pytest.raises(ValueError, match="Nested"):
            _render_template_simple(template, {"x": [1], "y": [2]})

    def test_missing_variable_returns_empty(self):
        assert _render_template_simple("{{ missing }}", {}) == ""


# --- render_markdown ---

class TestRenderMarkdown:
    def test_fallback_on_missing_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr("main.Path", lambda *a: tmp_path / "nope" if len(a) == 1 else type(tmp_path)(*a))
        result = render_markdown("READY", [], "repo")
        assert "READY" in result

    def test_uses_builtin_renderer_without_jinja(self):
        saved = sys.modules.get("jinja2")
        sys.modules["jinja2"] = None
        try:
            result = render_markdown("READY", [], "repo")
        except AttributeError:
            pytest.fail("Fallback did not catch jinja2 unavailability")
        finally:
            if saved is not None:
                sys.modules["jinja2"] = saved
            else:
                sys.modules.pop("jinja2", None)
        assert "READY" in result


# --- main (integration-level) ---

class TestMain:
    @patch("main.importlib.import_module")
    def test_all_pass_returns_0(self, mock_import):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(rule="test-rule", passed=True)
        fake_mod.detect_image_pattern.return_value = "static_csv"
        mock_import.return_value = fake_mod

        exit_code = main([".", "--rules", "csv,tags,egress,python", "--report", "json"])
        assert exit_code == 0

    @patch("main.importlib.import_module")
    def test_blocker_returns_1(self, mock_import):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(
            rule="test-rule", passed=False,
            findings=[Finding("blocker", "f.go", 1, "img", "fail")],
        )
        fake_mod.detect_image_pattern.return_value = "static_csv"
        mock_import.return_value = fake_mod

        exit_code = main([".", "--rules", "csv", "--report", "json"])
        assert exit_code == 1

    @patch("main.importlib.import_module")
    def test_output_flag_writes_file(self, mock_import, tmp_path):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(rule="r", passed=True)
        fake_mod.detect_image_pattern.return_value = "static_csv"
        mock_import.return_value = fake_mod

        out_file = tmp_path / "report.json"
        exit_code = main([".", "--rules", "csv", "--report", "json", "-o", str(out_file)])
        assert exit_code == 0
        content = out_file.read_text()
        data = json.loads(content.strip())
        assert data["score"] == "READY"

    def test_manifest_rule_triggers_adapt(self):
        fake_manifest = FakeManifest(images=[], components=[], known_issues=[])

        with patch("main.load_manifest", return_value=(fake_manifest, set())) as mock_load, \
             patch("main.adapt_manifest_result", return_value=RuleResult(rule="operator-manifest")) as mock_adapt, \
             patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock()
            exit_code = main([".", "--rules", "manifest", "--report", "json"])
            assert exit_code == 0
            mock_load.assert_called_once()
            mock_adapt.assert_called_once_with(fake_manifest)

    def test_env_var_pattern_triggers_manifest_load(self):
        fake_mod = MagicMock()
        fake_mod.detect_image_pattern.return_value = "env_var"
        fake_mod.run.return_value = RuleResult(rule="csv", passed=True)

        fake_manifest = FakeManifest(images=[], components=[], known_issues=[])

        with patch("main.load_manifest", return_value=(fake_manifest, set())) as mock_load, \
             patch("importlib.import_module", return_value=fake_mod):
            exit_code = main([".", "--rules", "csv", "--report", "json"])
            assert exit_code == 0
            mock_load.assert_called_once()
