"""Tests for rules/no_runtime_egress.py"""

from pathlib import Path

from rules.common import ProductionScope
from rules.no_runtime_egress import has_configurable_url, run


class TestHasConfigurableUrl:
    def test_go_getenv(self):
        assert has_configurable_url('url := os.Getenv("API_URL")') is True

    def test_python_environ(self):
        assert has_configurable_url('url = os.environ["API"]') is True

    def test_shell_expansion(self):
        assert has_configurable_url("curl ${API_URL}/health") is True

    def test_config_dot(self):
        assert has_configurable_url("endpoint = config.APIUrl") is True

    def test_process_env(self):
        assert has_configurable_url("const url = process.env.API") is True

    def test_viper(self):
        assert has_configurable_url('url := viper.GetString("api")') is True

    def test_hardcoded_url(self):
        assert has_configurable_url('requests.get("https://api.example.com")') is False


class TestRun:
    def test_empty_repo(self, tmp_path):
        result = run(str(tmp_path))
        assert result.passed is True
        assert result.findings == []
        assert result.rule == "no-runtime-egress"

    def test_go_hardcoded_url_is_blocker(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('resp, err := http.Get("https://api.external.com/data")')
        result = run(str(tmp_path))
        assert result.passed is False
        assert len(result.findings) == 1
        assert result.findings[0].severity == "blocker"
        assert "hardcoded" in result.findings[0].message

    def test_go_configurable_url_is_info(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('url := os.Getenv("URL"); http.Get(url)')
        result = run(str(tmp_path))
        assert result.passed is True
        assert any(f.severity == "info" for f in result.findings)

    def test_go_no_hardcoded_url_is_info(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text("http.Get(someVar)")
        result = run(str(tmp_path))
        assert result.passed is True
        assert any(f.severity == "info" for f in result.findings)
        assert any("no hardcoded URL" in f.message for f in result.findings)

    def test_python_requests_hardcoded_is_blocker(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "fetch.py"
        f.write_text('requests.get("https://example.com/api")')
        result = run(str(tmp_path))
        assert result.passed is False
        assert result.findings[0].severity == "blocker"

    def test_shell_curl_hardcoded_is_blocker(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        f = scripts / "run.sh"
        f.write_text("curl https://api.example.com/data")
        result = run(str(tmp_path))
        assert result.passed is False
        assert result.findings[0].severity == "blocker"

    def test_ts_fetch_hardcoded_is_blocker(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "api.ts"
        f.write_text('fetch("https://api.example.com/v1")')
        result = run(str(tmp_path))
        assert result.passed is False
        assert result.findings[0].severity == "blocker"

    def test_tsx_axios_hardcoded_is_blocker(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "comp.tsx"
        f.write_text('axios.get("https://api.example.com")')
        result = run(str(tmp_path))
        assert result.passed is False

    def test_go_comment_skipped(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('// http.Get("https://api.external.com/data")')
        result = run(str(tmp_path))
        assert result.findings == []

    def test_python_comment_skipped(self, tmp_path):
        f = tmp_path / "fetch.py"
        f.write_text('# requests.get("https://example.com")')
        result = run(str(tmp_path))
        assert result.findings == []

    def test_build_context_skipped(self, tmp_path):
        f = tmp_path / "Dockerfile"
        f.write_text("RUN curl https://example.com/install.sh")
        result = run(str(tmp_path))
        assert result.findings == []

    def test_test_dir_produces_blocker(self, tmp_path):
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        f = test_dir / "helper.py"
        f.write_text('requests.get("https://example.com")')
        result = run(str(tmp_path))
        assert len(result.findings) == 1
        assert result.findings[0].severity == "blocker"
        assert result.passed is False

    def test_unrecognized_extension_skipped(self, tmp_path):
        f = tmp_path / "file.rb"
        f.write_text('Net::HTTP.get("https://example.com")')
        result = run(str(tmp_path))
        assert result.findings == []

    def test_vendor_dir_skipped(self, tmp_path):
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        f = vendor / "dep.go"
        f.write_text('http.Get("https://example.com")')
        result = run(str(tmp_path))
        assert result.findings == []

    def test_unreadable_file_skipped(self, tmp_path):
        f = tmp_path / "bad.go"
        f.write_bytes(b'\x80\x81\x82' * 100)
        result = run(str(tmp_path))
        assert result.findings == []

    def test_net_dial_detected(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "conn.go"
        f.write_text('conn, err := net.Dial("tcp", "example.com:443")')
        result = run(str(tmp_path))
        assert len(result.findings) == 1
        assert result.findings[0].severity == "info"
        assert "no hardcoded URL" in result.findings[0].message

    def test_production_scope_downgrades_blocker(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('resp, err := http.Get("https://api.external.com/data")')
        other = tmp_path / "main.go"
        other.write_text("package main\n")
        scope = ProductionScope(
            production_files={other.resolve()}, method="go-import-graph",
        )
        result = run(str(tmp_path), production_scope=scope)
        assert result.passed is True
        assert result.findings[0].severity == "info"
        assert "[out of production scope]" in result.findings[0].message

    def test_production_scope_keeps_in_scope_blocker(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('resp, err := http.Get("https://api.external.com/data")')
        scope = ProductionScope(
            production_files={f.resolve()},
            method="go-import-graph",
        )
        result = run(str(tmp_path), production_scope=scope)
        assert result.passed is False
        assert result.findings[0].severity == "blocker"

    def test_production_scope_none_no_change(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('resp, err := http.Get("https://api.external.com/data")')
        result = run(str(tmp_path), production_scope=None)
        assert result.passed is False
        assert result.findings[0].severity == "blocker"

    def test_production_scope_ignores_non_go(self, tmp_path):
        f = tmp_path / "fetch.py"
        f.write_text('requests.get("https://example.com/api")')
        scope = ProductionScope(production_files=set(), method="go-import-graph")
        result = run(str(tmp_path), production_scope=scope)
        assert result.findings[0].severity == "blocker"

    def test_kubernetes_internal_url_is_info(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('resp, err := http.Get("https://kubernetes.default.svc/api/v1")')
        result = run(str(tmp_path))
        assert result.passed is True
        assert result.findings[0].severity == "info"
        assert "cluster-internal" in result.findings[0].message

    def test_svc_cluster_local_url_is_info(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('resp, err := http.Get("https://my-svc.ns.svc.cluster.local:8080")')
        result = run(str(tmp_path))
        assert result.passed is True
        assert result.findings[0].severity == "info"

    def test_localhost_url_is_info(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text('resp, err := http.Get("http://localhost:8080/health")')
        result = run(str(tmp_path))
        assert result.passed is True
        assert result.findings[0].severity == "info"
    def test_hf_download_in_shell_detected(self, tmp_path):
        f = tmp_path / "setup.sh"
        f.write_text("hf download ibm-granite/granite-embedding-125m-english")
        result = run(str(tmp_path))
        assert result.passed is False
        assert len(result.findings) == 1
        assert "HuggingFace" in result.findings[0].message

    def test_huggingface_cli_download_in_shell_detected(self, tmp_path):
        f = tmp_path / "setup.sh"
        f.write_text("huggingface-cli download google/flan-t5-small")
        result = run(str(tmp_path))
        assert result.passed is False
        assert len(result.findings) == 1
        assert result.findings[0].severity == "blocker"
        assert "HuggingFace" in result.findings[0].message

    def test_hf_download_subprocess_in_python_detected(self, tmp_path):
        f = tmp_path / "build.py"
        f.write_text('subprocess.run(["hf", "download", "model-name"])')
        result = run(str(tmp_path))
        assert result.passed is False
        assert len(result.findings) == 1
        assert result.findings[0].severity == "blocker"
        assert "HuggingFace" in result.findings[0].message
