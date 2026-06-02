"""Tests for rules/no_image_tags.py"""

from pathlib import Path

from rules.no_image_tags import is_excluded_file, is_source_code, scan_file, run


class TestIsExcludedFile:
    def test_semgrep_yaml(self):
        assert is_excluded_file(Path("semgrep.yaml")) is True

    def test_params_env(self):
        assert is_excluded_file(Path("manifests/params.env")) is True

    def test_test_suffix(self):
        assert is_excluded_file(Path("pkg/foo_test.go")) is True

    def test_int_test_suffix(self):
        assert is_excluded_file(Path("pkg/foo_int_test.go")) is True

    def test_test_dir(self):
        assert is_excluded_file(Path("test/helper.go")) is True

    def test_e2e_dir(self):
        assert is_excluded_file(Path("e2e/suite.go")) is True

    def test_ci_dir(self):
        assert is_excluded_file(Path(".github/workflows/ci.yaml")) is True

    def test_tekton_dir(self):
        assert is_excluded_file(Path(".tekton/pipeline.yaml")) is True

    def test_dockerfile(self):
        assert is_excluded_file(Path("Dockerfile")) is True

    def test_containerfile(self):
        assert is_excluded_file(Path("build/Containerfile")) is True

    def test_named_dockerfile(self):
        assert is_excluded_file(Path("build/runtime.Dockerfile")) is True

    def test_regular_file(self):
        assert is_excluded_file(Path("pkg/server.go")) is False


class TestIsSourceCode:
    def test_go_file(self):
        assert is_source_code(Path("pkg/main.go")) is True

    def test_python_file(self):
        assert is_source_code(Path("src/app.py")) is True

    def test_shell_file(self):
        assert is_source_code(Path("scripts/run.sh")) is True

    def test_yaml_file(self):
        assert is_source_code(Path("config/deploy.yaml")) is False

    def test_json_file(self):
        assert is_source_code(Path("config/settings.json")) is False


class TestScanFile:
    def test_digest_ref_skipped(self, tmp_path):
        f = tmp_path / "deploy.yaml"
        f.write_text("image: quay.io/org/img@sha256:" + "a" * 64)
        assert scan_file(f, tmp_path) == []

    def test_tag_ref_in_source_is_blocker(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "main.go"
        f.write_text('image: quay.io/org/img:latest')
        findings = scan_file(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"
        assert ":latest" in findings[0].image

    def test_tag_ref_in_manifest_is_blocker(self, tmp_path):
        manifests = tmp_path / "manifests"
        manifests.mkdir()
        f = manifests / "deploy.yaml"
        f.write_text('image: quay.io/org/img:v1.0')
        findings = scan_file(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"

    def test_tag_ref_in_test_dir_is_info(self, tmp_path):
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        f = test_dir / "helper.go"
        f.write_text('image: quay.io/org/img:v1.0')
        findings = scan_file(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "info"

    def test_tag_ref_in_test_go_file_is_info(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "handler_test.go"
        f.write_text('image: quay.io/org/img:v1.0')
        findings = scan_file(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "info"

    def test_tag_ref_in_python_source_is_blocker(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text('image = "quay.io/org/img:latest"')
        findings = scan_file(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"

    def test_hash_comment_skipped(self, tmp_path):
        f = tmp_path / "deploy.yaml"
        f.write_text('# image: quay.io/org/img:v1')
        assert scan_file(f, tmp_path) == []

    def test_slash_comment_skipped(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text('// image: quay.io/org/img:v1')
        assert scan_file(f, tmp_path) == []

    def test_https_url_skipped(self, tmp_path):
        f = tmp_path / "go.mod"
        f.write_text('require https://github.com/kubernetes/api:v0.28.0')
        assert scan_file(f, tmp_path) == []

    def test_http_url_skipped(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text('url := "http://registry.example.com/org/img:v1"')
        assert scan_file(f, tmp_path) == []

    def test_image_ref_not_url_still_detected(self, tmp_path):
        f = tmp_path / "deploy.yaml"
        f.write_text('image: quay.io/org/img:v1')
        findings = scan_file(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].image == "quay.io/org/img:v1"

    def test_unreadable_file(self, tmp_path):
        f = tmp_path / "binary.go"
        f.write_bytes(b'\x80\x81\x82' * 100)
        assert scan_file(f, tmp_path) == []

    def test_finding_has_correct_line_number(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text("line1\nline2\nimage: quay.io/org/img:v1\nline4")
        findings = scan_file(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].line == 3

    def test_finding_has_relative_path(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('image: quay.io/org/img:latest')
        findings = scan_file(f, tmp_path)
        assert findings[0].file == "pkg/client.go"


class TestRun:
    def test_empty_repo(self, tmp_path):
        result = run(str(tmp_path))
        assert result.passed is True
        assert result.findings == []
        assert result.rule == "no-image-tags"

    def test_skips_git_dir(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        f = git_dir / "config"
        f.write_text('image: quay.io/org/img:latest')
        result = run(str(tmp_path))
        assert result.findings == []

    def test_skips_vendor_dir(self, tmp_path):
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        f = vendor / "dep.go"
        f.write_text('image: quay.io/org/img:latest')
        result = run(str(tmp_path))
        assert result.findings == []

    def test_skips_non_matching_extension(self, tmp_path):
        f = tmp_path / "readme.txt"
        f.write_text('image: quay.io/org/img:latest')
        result = run(str(tmp_path))
        assert result.findings == []

    def test_dockerfile_demoted_to_info(self, tmp_path):
        f = tmp_path / "Dockerfile"
        f.write_text('FROM quay.io/org/base:latest')
        result = run(str(tmp_path))
        assert len(result.findings) == 1
        assert result.findings[0].severity == "info"
        assert result.passed is True

    def test_manifest_tag_sets_passed_false(self, tmp_path):
        manifests = tmp_path / "manifests"
        manifests.mkdir()
        f = manifests / "deploy.yaml"
        f.write_text('image: quay.io/org/img:v1.0')
        result = run(str(tmp_path))
        assert result.passed is False
        assert result.findings[0].severity == "blocker"

    def test_source_code_tag_sets_passed_false(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "main.go"
        f.write_text('image: quay.io/org/img:latest')
        result = run(str(tmp_path))
        assert result.passed is False
        assert any(f.severity == "blocker" for f in result.findings)

    def test_mixed_findings(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        test_dir = tmp_path / "test"
        test_dir.mkdir()

        (pkg / "main.go").write_text(
            'image: quay.io/org/img@sha256:' + 'a' * 64
        )
        (test_dir / "helper.go").write_text('image: quay.io/org/img:v1')

        result = run(str(tmp_path))
        assert result.passed is True
        severities = {f.severity for f in result.findings}
        assert "info" in severities
        assert "blocker" not in severities
