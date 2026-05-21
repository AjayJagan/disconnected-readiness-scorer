from dataclasses import dataclass, field


@dataclass
class Finding:
    severity: str
    file: str
    line: int
    image: str
    message: str


@dataclass
class RuleResult:
    rule: str
    passed: bool = True
    findings: list[Finding] = field(default_factory=list)
