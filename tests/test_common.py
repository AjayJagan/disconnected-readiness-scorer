"""Tests for rules.common dataclasses."""

from rules.common import Finding, RuleResult


class TestFinding:
    def test_fields(self):
        f = Finding(severity="blocker", file="foo.go", line=10, image="quay.io/x:latest", message="bad tag")
        assert f.severity == "blocker"
        assert f.file == "foo.go"
        assert f.line == 10
        assert f.image == "quay.io/x:latest"
        assert f.message == "bad tag"

    def test_equality(self):
        a = Finding("warning", "a.py", 1, "", "msg")
        b = Finding("warning", "a.py", 1, "", "msg")
        assert a == b

    def test_inequality(self):
        a = Finding("blocker", "a.py", 1, "", "msg")
        b = Finding("warning", "a.py", 1, "", "msg")
        assert a != b


class TestRuleResult:
    def test_defaults(self):
        r = RuleResult(rule="test-rule")
        assert r.rule == "test-rule"
        assert r.passed is True
        assert r.findings == []

    def test_mutable_default_isolation(self):
        r1 = RuleResult(rule="a")
        r2 = RuleResult(rule="b")
        r1.findings.append(Finding("info", "", 0, "", "x"))
        assert r2.findings == []

    def test_passed_override(self):
        r = RuleResult(rule="x", passed=False)
        assert r.passed is False

    def test_findings_provided(self):
        findings = [Finding("blocker", "f.go", 5, "img", "m")]
        r = RuleResult(rule="x", passed=False, findings=findings)
        assert len(r.findings) == 1
        assert r.findings[0].severity == "blocker"
