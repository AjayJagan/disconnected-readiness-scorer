"""Tests for rules/no_runtime_egress.py"""

from pathlib import Path

from rules.no_runtime_egress import is_build_context, has_configurable_url, run


class TestIsBuildContext:
    def test_dockerfile(self):
        assert is_build_context(Path("Dockerfile")) is True

    def test_makefile(self):
        assert is_build_context(Path("Makefile")) is True

    def test_containerfile(self):
        assert is_build_context(Path("Containerfile")) is True

    def test_github_dir(self):
        assert is_build_context(Path(".github/workflows/ci.yaml")) is True

    def test_hack_dir(self):
        assert is_build_context(Path("hack/build.sh")) is True

    def test_ci_dir(self):
        assert is_build_context(Path("ci/run.sh")) is True

    def test_regular_source(self):
        assert is_build_context(Path("pkg/server.go")) is False


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

    def test_go_no_hardcoded_url_is_warning(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f = pkg / "client.go"
        f.write_text("http.Get(someVar)")
        result = run(str(tmp_path))
        assert result.passed is True
        assert any(f.severity == "warning" for f in result.findings)

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

    def test_test_dir_skipped(self, tmp_path):
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        f = test_dir / "helper.py"
        f.write_text('requests.get("https://example.com")')
        result = run(str(tmp_path))
        assert result.findings == []

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
        assert result.findings[0].severity == "warning"
