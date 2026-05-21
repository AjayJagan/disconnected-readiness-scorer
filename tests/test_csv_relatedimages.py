"""Tests for rules/csv_relatedimages.py"""

from pathlib import Path

from rules.csv_relatedimages import (
    is_excluded_file, normalize_image, detect_image_pattern,
    extract_related_image_vars, extract_static_related_images,
    scan_for_image_refs, check_env_var_pattern, check_static_csv_pattern, run,
)


class TestIsExcludedFile:
    def test_semgrep_yaml(self):
        assert is_excluded_file(Path("semgrep.yaml")) is True

    def test_test_suffix(self):
        assert is_excluded_file(Path("pkg/foo_test.go")) is True

    def test_e2e_dir(self):
        assert is_excluded_file(Path("e2e/suite.go")) is True

    def test_ci_dir(self):
        assert is_excluded_file(Path(".github/workflow.yaml")) is True

    def test_hack_dir(self):
        assert is_excluded_file(Path("hack/script.go")) is True

    def test_regular_file(self):
        assert is_excluded_file(Path("pkg/main.go")) is False


class TestNormalizeImage:
    def test_strips_digest(self):
        result = normalize_image("quay.io/org/img@sha256:" + "a" * 64)
        assert result == "quay.io/org/img"

    def test_strips_tag(self):
        assert normalize_image("quay.io/org/img:v1.2.3") == "quay.io/org/img"

    def test_strips_double_quotes(self):
        assert normalize_image('"quay.io/org/img:v1"') == "quay.io/org/img"

    def test_strips_single_quotes(self):
        assert normalize_image("'quay.io/org/img:v1'") == "quay.io/org/img"

    def test_already_clean(self):
        assert normalize_image("quay.io/org/img") == "quay.io/org/img"

    def test_strips_whitespace(self):
        assert normalize_image("  quay.io/org/img:v1  ") == "quay.io/org/img"


class TestDetectImagePattern:
    def _write_go_files_with_related_images(self, tmp_path, count):
        pkg = tmp_path / "pkg"
        pkg.mkdir(exist_ok=True)
        lines = [f'"RELATED_IMAGE_IMG_{i}"' for i in range(count)]
        (pkg / "images.go").write_text("\n".join(lines))

    def test_env_var_pattern_detected(self, tmp_path):
        self._write_go_files_with_related_images(tmp_path, 6)
        assert detect_image_pattern(tmp_path) == "env_var"

    def test_below_threshold_not_env_var(self, tmp_path):
        self._write_go_files_with_related_images(tmp_path, 3)
        assert detect_image_pattern(tmp_path) != "env_var"

    def test_static_csv_detected(self, tmp_path):
        f = tmp_path / "csv.yaml"
        f.write_text(
            "kind: ClusterServiceVersion\n"
            "spec:\n"
            "  relatedImages:\n"
            "    - name: img\n"
            "      image: quay.io/org/img@sha256:" + "a" * 64 + "\n"
        )
        assert detect_image_pattern(tmp_path) == "static_csv"

    def test_unknown_pattern(self, tmp_path):
        assert detect_image_pattern(tmp_path) == "unknown"

    def test_skips_vendor_go_files(self, tmp_path):
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        lines = [f'"RELATED_IMAGE_IMG_{i}"' for i in range(10)]
        (vendor / "dep.go").write_text("\n".join(lines))
        assert detect_image_pattern(tmp_path) == "unknown"


class TestExtractRelatedImageVars:
    def test_extracts_from_go(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text(
            'os.Getenv("RELATED_IMAGE_FOO")\nos.Getenv("RELATED_IMAGE_BAR")'
        )
        result = extract_related_image_vars(tmp_path)
        assert result == {"RELATED_IMAGE_FOO", "RELATED_IMAGE_BAR"}

    def test_skips_wildcard(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "util.go").write_text('"RELATED_IMAGE_*"')
        assert extract_related_image_vars(tmp_path) == set()

    def test_skips_test_files(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "foo_test.go").write_text('"RELATED_IMAGE_FOO"')
        assert extract_related_image_vars(tmp_path) == set()

    def test_skips_excluded_dirs(self, tmp_path):
        e2e = tmp_path / "e2e"
        e2e.mkdir()
        (e2e / "setup.go").write_text('"RELATED_IMAGE_FOO"')
        assert extract_related_image_vars(tmp_path) == set()

    def test_skips_vendor(self, tmp_path):
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "dep.go").write_text('"RELATED_IMAGE_FOO"')
        assert extract_related_image_vars(tmp_path) == set()

    def test_deduplicates(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "a.go").write_text('"RELATED_IMAGE_FOO"')
        (pkg / "b.go").write_text('"RELATED_IMAGE_FOO"')
        assert extract_related_image_vars(tmp_path) == {"RELATED_IMAGE_FOO"}


class TestExtractStaticRelatedImages:
    def test_extracts_images(self, tmp_path):
        f = tmp_path / "csv.yaml"
        f.write_text(
            "spec:\n"
            "  relatedImages:\n"
            "    - name: img\n"
            "      image: quay.io/org/img:v1\n"
        )
        result = extract_static_related_images(tmp_path)
        assert "quay.io/org/img" in result

    def test_skips_non_related_yaml(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("key: value\n")
        assert extract_static_related_images(tmp_path) == set()

    def test_skips_vendor(self, tmp_path):
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        f = vendor / "csv.yaml"
        f.write_text(
            "spec:\n"
            "  relatedImages:\n"
            "    - name: img\n"
            "      image: quay.io/org/img:v1\n"
        )
        assert extract_static_related_images(tmp_path) == set()


class TestScanForImageRefs:
    def test_finds_image_in_yaml(self, tmp_path):
        f = tmp_path / "deploy.yaml"
        f.write_text("image: quay.io/org/img:v1")
        refs = scan_for_image_refs(tmp_path)
        assert len(refs) == 1
        assert refs[0][2] == "quay.io/org/img:v1"

    def test_finds_from_in_dockerfile(self, tmp_path):
        f = tmp_path / "Dockerfile"
        f.write_text("FROM quay.io/org/base:latest")
        refs = scan_for_image_refs(tmp_path)
        assert len(refs) == 1

    def test_skips_git_dir(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "config.yaml").write_text("image: quay.io/org/img:v1")
        assert scan_for_image_refs(tmp_path) == []

    def test_skips_wrong_extension(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("image: quay.io/org/img:v1")
        assert scan_for_image_refs(tmp_path) == []


class TestCheckEnvVarPattern:
    def _make_env_var_repo(self, tmp_path, go_content, yaml_content=None):
        pkg = tmp_path / "pkg"
        pkg.mkdir(exist_ok=True)
        (pkg / "images.go").write_text(go_content)
        if yaml_content:
            (tmp_path / "deploy.yaml").write_text(yaml_content)

    def test_no_manifest_info_message(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text('"RELATED_IMAGE_FOO"')
        result = check_env_var_pattern(tmp_path)
        assert result.passed is True
        info = [f for f in result.findings if f.severity == "info"]
        assert any("Found 1 env vars" in f.message for f in info)

    def test_with_manifest_info_message(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text('"RELATED_IMAGE_FOO"')
        result = check_env_var_pattern(tmp_path, manifest_env_vars={"RELATED_IMAGE_FOO"})
        info = [f for f in result.findings if f.severity == "info"]
        assert any("validated against" in f.message for f in info)

    def test_unmapped_image_is_warning(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text('"RELATED_IMAGE_FOO"')
        (tmp_path / "deploy.yaml").write_text("image: quay.io/org/unmapped:v1")
        result = check_env_var_pattern(tmp_path)
        warnings = [f for f in result.findings if f.severity == "warning"]
        assert any("no RELATED_IMAGE_*" in f.message for f in warnings)

    def test_unmapped_image_in_excluded_is_info(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text('"RELATED_IMAGE_FOO"')
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        (test_dir / "helper.yaml").write_text("image: quay.io/org/unmapped:v1")
        result = check_env_var_pattern(tmp_path)
        test_findings = [f for f in result.findings if f.file.startswith("test/")]
        assert all(f.severity == "info" for f in test_findings)

    def test_var_not_in_manifest_is_blocker(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text(
            'image: quay.io/org/img:v1  // RELATED_IMAGE_MISSING'
        )
        result = check_env_var_pattern(
            tmp_path, manifest_env_vars={"RELATED_IMAGE_OTHER"}
        )
        blockers = [f for f in result.findings if f.severity == "blocker"]
        assert len(blockers) >= 1
        assert result.passed is False

    def test_var_not_in_manifest_excluded_is_info(self, tmp_path):
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        (test_dir / "helper.go").write_text(
            'image: quay.io/org/img:v1  // RELATED_IMAGE_MISSING'
        )
        result = check_env_var_pattern(
            tmp_path, manifest_env_vars={"RELATED_IMAGE_OTHER"}
        )
        test_findings = [f for f in result.findings if f.file.startswith("test/")]
        assert all(f.severity == "info" for f in test_findings)
        assert result.passed is True

    def test_stale_vars_warning(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text('"RELATED_IMAGE_STALE"')
        result = check_env_var_pattern(
            tmp_path, manifest_env_vars={"RELATED_IMAGE_OTHER"}
        )
        warnings = [f for f in result.findings if f.severity == "warning"]
        assert any("stale" in f.message.lower() for f in warnings)

    def test_unused_manifest_vars_info(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text('"RELATED_IMAGE_FOO"')
        result = check_env_var_pattern(
            tmp_path,
            manifest_env_vars={"RELATED_IMAGE_FOO", "RELATED_IMAGE_EXTRA"},
        )
        info = [f for f in result.findings if "not referenced" in f.message]
        assert len(info) == 1

    def test_all_vars_match_passes(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "images.go").write_text('"RELATED_IMAGE_FOO"')
        result = check_env_var_pattern(
            tmp_path, manifest_env_vars={"RELATED_IMAGE_FOO"}
        )
        assert result.passed is True


class TestCheckStaticCsvPattern:
    def test_empty_related_images_warning(self, tmp_path):
        result = check_static_csv_pattern(tmp_path)
        warnings = [f for f in result.findings if f.severity == "warning"]
        assert any("empty or unparseable" in f.message for f in warnings)

    def test_missing_image_is_blocker(self, tmp_path):
        f = tmp_path / "csv.yaml"
        f.write_text(
            "spec:\n"
            "  relatedImages:\n"
            "    - name: known\n"
            "      image: quay.io/org/known:v1\n"
        )
        (tmp_path / "deploy.yaml").write_text("image: quay.io/org/missing:v1")
        result = check_static_csv_pattern(tmp_path)
        assert result.passed is False
        blockers = [f for f in result.findings if f.severity == "blocker"]
        assert len(blockers) >= 1

    def test_excluded_missing_image_is_info(self, tmp_path):
        f = tmp_path / "csv.yaml"
        f.write_text(
            "spec:\n"
            "  relatedImages:\n"
            "    - name: known\n"
            "      image: quay.io/org/known:v1\n"
        )
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        (test_dir / "helper.yaml").write_text("image: quay.io/org/missing:v1")
        result = check_static_csv_pattern(tmp_path)
        assert result.passed is True
        test_findings = [f for f in result.findings if f.file.startswith("test/")]
        assert all(f.severity == "info" for f in test_findings)

    def test_all_images_covered(self, tmp_path):
        f = tmp_path / "csv.yaml"
        f.write_text(
            "spec:\n"
            "  relatedImages:\n"
            "    - name: img\n"
            "      image: quay.io/org/img:v1\n"
        )
        (tmp_path / "deploy.yaml").write_text("image: quay.io/org/img:v2")
        result = check_static_csv_pattern(tmp_path)
        assert result.passed is True


class TestRun:
    def test_unknown_pattern_returns_info(self, tmp_path):
        result = run(str(tmp_path))
        assert result.rule == "image-manifest-complete"
        assert result.passed is True
        assert any("Cannot determine" in f.message for f in result.findings)

    def test_env_var_pattern_dispatches(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        lines = [f'"RELATED_IMAGE_IMG_{i}"' for i in range(6)]
        (pkg / "images.go").write_text("\n".join(lines))
        result = run(str(tmp_path))
        assert any("RELATED_IMAGE_*" in f.message for f in result.findings)

    def test_static_csv_pattern_dispatches(self, tmp_path):
        f = tmp_path / "csv.yaml"
        f.write_text(
            "kind: ClusterServiceVersion\n"
            "spec:\n"
            "  relatedImages:\n"
            "    - name: img\n"
            "      image: quay.io/org/img:v1\n"
        )
        result = run(str(tmp_path))
        assert result.rule == "image-manifest-complete"

    def test_manifest_env_vars_passed_through(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        lines = [f'"RELATED_IMAGE_IMG_{i}"' for i in range(6)]
        (pkg / "images.go").write_text("\n".join(lines))
        result = run(
            str(tmp_path),
            manifest_env_vars={"RELATED_IMAGE_IMG_0", "RELATED_IMAGE_IMG_1"},
        )
        assert any("validated against" in f.message for f in result.findings)
