import json
from pathlib import Path

from rules.common import ArchAnalyzerResult

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arch_analyzer"


def load_arch_fixture(name: str) -> ArchAnalyzerResult:
    path = _FIXTURES_DIR / f"{name}.json"
    return ArchAnalyzerResult.from_dict(json.loads(path.read_text()))
