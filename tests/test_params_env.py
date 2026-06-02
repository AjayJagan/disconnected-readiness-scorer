"""Tests for params_env pattern detection and validation."""

from unittest.mock import patch

from rules.params_env import run, detect_params_env
from rules.params_env_utils import (
    parse_params_env,
    _looks_like_image,
    discover_overlays,
    load_ignore_file,
    find_go_related_image_envs,
)


# --- _looks_like_image ---

class TestLooksLikeImage:
    def test_registry_image(self):
        assert _looks_like_image("quay.io/org/repo:tag") is True

    def test_digest_image(self):
        assert _looks_like_image("quay.io/org/repo@sha256:abc123") is True

    def test_no_slash(self):
        assert _looks_like_image("just-a-name") is False

    def test_absolute_path(self):
        assert _looks_like_image("/usr/local/bin") is False

    def test_relative_path(self):
        assert _looks_like_image("./local/path") is False


# --- parse_params_env ---

class TestParseParamsEnv:
    def test_basic(self, tmp_path):
        f = tmp_path / "params.env"
        f.write_text("odh-model-controller=quay.io/org/ctrl@sha256:abc\n")
        result = parse_params_env(f)
        assert result == {"odh-model-controller": "quay.io/org/ctrl@sha256:abc"}

    def test_skips_comments_and_blanks(self, tmp_path):
        f = tmp_path / "params.env"
        f.write_text("# comment\n\nKEY=quay.io/org/img:tag\n")
        result = parse_params_env(f)
        assert "KEY" in result

    def test_skips_non_image(self, tmp_path):
        f = tmp_path / "params.env"
        f.write_text("LOG_LEVEL=debug\n")
        assert parse_params_env(f) == {}

    def test_missing_file(self, tmp_path):
        assert parse_params_env(tmp_path / "nope.env") == {}

    def test_no_equals(self, tmp_path):
        f = tmp_path / "params.env"
        f.write_text("no-equals-here\n")
        assert parse_params_env(f) == {}


# --- discover_overlays ---

class TestDiscoverOverlays:
    def test_finds_overlay_with_params_env_and_kustomization(self, tmp_path):
        overlay = tmp_path / "config" / "overlays" / "default"
        overlay.mkdir(parents=True)
        (overlay / "params.env").write_text("IMG=quay.io/org/img:tag\n")
        (overlay / "kustomization.yaml").write_text("resources:\n- ../base\n")
        result = discover_overlays(tmp_path)
        assert len(result) == 1
        assert result[0] == overlay

    def test_skips_params_env_without_kustomization(self, tmp_path):
        d = tmp_path / "no-kustomize"
        d.mkdir()
        (d / "params.env").write_text("IMG=quay.io/org/img:tag\n")
        assert discover_overlays(tmp_path) == []

    def test_skips_vendor(self, tmp_path):
        d = tmp_path / "vendor" / "overlay"
        d.mkdir(parents=True)
        (d / "params.env").write_text("IMG=quay.io/org/img:tag\n")
        (d / "kustomization.yaml").write_text("resources: []\n")
        assert discover_overlays(tmp_path) == []


# --- load_ignore_file ---

class TestLoadIgnoreFile:
    def test_loads_keys(self, tmp_path):
        (tmp_path / ".verify-params-env-ignore").write_text(
            "exceptions:\n"
            "  - key: odh-model-controller\n"
            "    reason: managed by operator\n"
        )
        keys = load_ignore_file(tmp_path)
        assert keys == {"odh-model-controller"}

    def test_missing_file(self, tmp_path):
        assert load_ignore_file(tmp_path) == set()

    def test_missing_reason_warns(self, tmp_path, capsys):
        (tmp_path / ".verify-params-env-ignore").write_text(
            "exceptions:\n  - key: no-reason\n"
        )
        keys = load_ignore_file(tmp_path)
        assert keys == set()
        assert "missing 'reason'" in capsys.readouterr().err


# --- find_go_related_image_envs ---

class TestFindGoRelatedImageEnvs:
    def test_finds_getenv(self, tmp_path):
        go_file = tmp_path / "main.go"
        go_file.write_text('package main\nvar x = os.Getenv("RELATED_IMAGE_FOO")\n')
        assert find_go_related_image_envs(tmp_path) == {"RELATED_IMAGE_FOO"}

    def test_skips_test_files(self, tmp_path):
        go_file = tmp_path / "main_test.go"
        go_file.write_text('package main\nvar x = os.Getenv("RELATED_IMAGE_BAR")\n')
        assert find_go_related_image_envs(tmp_path) == set()

    def test_nonexistent_dir(self, tmp_path):
        assert find_go_related_image_envs(tmp_path / "nope") == set()


# --- detect_params_env ---

class TestDetectParamsEnvPattern:
    def test_detects_params_env(self, tmp_path):
        overlay = tmp_path / "config" / "default"
        overlay.mkdir(parents=True)
        (overlay / "params.env").write_text("IMG=quay.io/org/ctrl@sha256:abc\n")
        (overlay / "kustomization.yaml").write_text("resources: []\n")
        assert detect_params_env(tmp_path) is True

    def test_no_params_env(self, tmp_path):
        assert detect_params_env(tmp_path) is False

    def test_params_env_without_kustomization(self, tmp_path):
        d = tmp_path / "somedir"
        d.mkdir()
        (d / "params.env").write_text("IMG=quay.io/org/ctrl@sha256:abc\n")
        assert detect_params_env(tmp_path) is False

    def test_params_env_no_image_values(self, tmp_path):
        overlay = tmp_path / "config" / "default"
        overlay.mkdir(parents=True)
        (overlay / "params.env").write_text("LOG_LEVEL=debug\nNAMESPACE=foo\n")
        (overlay / "kustomization.yaml").write_text("resources: []\n")
        assert detect_params_env(tmp_path) is False


# --- run ---

class TestCheckParamsEnvPattern:
    def _make_overlay(self, tmp_path, params_content, kustomization_content="resources: []\n"):
        overlay = tmp_path / "config" / "default"
        overlay.mkdir(parents=True)
        (overlay / "params.env").write_text(params_content)
        (overlay / "kustomization.yaml").write_text(kustomization_content)
        return overlay

    def test_kustomize_unavailable_returns_info(self, tmp_path):
        self._make_overlay(tmp_path, "IMG=quay.io/org/img@sha256:abc123\n")
        with patch("rules.params_env.kustomize_available", return_value=False):
            result = run(str(tmp_path))
        assert result.passed is True
        kustomize_finding = next(f for f in result.findings if "kustomize not found" in f.message)
        assert kustomize_finding.severity == "info"

    def test_probe_detects_hardcoded_image(self, tmp_path):
        self._make_overlay(tmp_path, "IMG=quay.io/org/img@sha256:" + "a" * 64 + "\n")

        rendered_with_hardcoded = (
            "---\nkind: Deployment\nmetadata:\n  name: myapp\n"
            "spec:\n  image: registry.io/hardcoded/image:v1\n"
        )
        with patch("rules.params_env.kustomize_available", return_value=True), \
             patch("rules.params_env.kustomize_build", return_value=rendered_with_hardcoded), \
             patch("rules.params_env.create_probe_overlay", return_value=tmp_path):
            result = run(str(tmp_path))
        assert result.passed is False
        blockers = [f for f in result.findings if f.severity == "blocker"]
        assert any("Hardcoded image" in f.message for f in blockers)

    def test_go_orphan_getenv_is_blocker(self, tmp_path):
        self._make_overlay(tmp_path, "IMG=quay.io/org/img@sha256:" + "a" * 64 + "\n")
        go_file = tmp_path / "main.go"
        go_file.write_text('package main\nvar x = os.Getenv("RELATED_IMAGE_ORPHAN")\n')

        with patch("rules.params_env.kustomize_available", return_value=True), \
             patch("rules.params_env.kustomize_build", return_value="---\n"):
            result = run(str(tmp_path))
        assert result.passed is False
        blockers = [f for f in result.findings if f.severity == "blocker"]
        assert any("RELATED_IMAGE_ORPHAN" in f.message for f in blockers)

    def test_operator_manifest_cross_ref_blocker(self, tmp_path):
        self._make_overlay(tmp_path, "IMG=quay.io/org/img@sha256:" + "a" * 64 + "\n")

        rendered = (
            "---\nkind: Deployment\nmetadata:\n  name: ctrl\n"
            "spec:\n  containers:\n"
            "  - name: ctrl\n    env:\n"
            "    - name: RELATED_IMAGE_FOO\n"
            "      valueFrom:\n"
            "        configMapKeyRef:\n"
            "          key: IMG\n"
            "          name: params\n"
        )
        with patch("rules.params_env.kustomize_available", return_value=True), \
             patch("rules.params_env.kustomize_build", return_value=rendered):
            result = run(
                str(tmp_path),
                manifest_env_vars={"RELATED_IMAGE_BAR"},
            )
        assert result.passed is False
        blockers = [f for f in result.findings if f.severity == "blocker"]
        assert any("not in the operator manifest" in f.message for f in blockers)

    def test_ignore_file_excludes_key(self, tmp_path):
        self._make_overlay(tmp_path, "IGNORED=quay.io/org/img:v1.0\n")
        (tmp_path / ".verify-params-env-ignore").write_text(
            "exceptions:\n  - key: IGNORED\n    reason: managed externally\n"
        )
        with patch("rules.params_env.kustomize_available", return_value=True), \
             patch("rules.params_env.kustomize_build", return_value="---\n"):
            result = run(str(tmp_path))
        assert not any("IGNORED" in f.message and f.severity == "blocker" for f in result.findings)


# --- run() dispatcher ---

class TestRunDispatcher:
    def test_dispatches_to_params_env(self, tmp_path):
        overlay = tmp_path / "config" / "default"
        overlay.mkdir(parents=True)
        (overlay / "params.env").write_text("IMG=quay.io/org/img@sha256:" + "a" * 64 + "\n")
        (overlay / "kustomization.yaml").write_text("resources: []\n")

        with patch("rules.params_env.kustomize_available", return_value=True), \
             patch("rules.params_env.kustomize_build", return_value="---\n"):
            result = run(str(tmp_path))
        assert result.rule == "params-env-wiring"
        assert any("params.env pattern" in f.message for f in result.findings)
