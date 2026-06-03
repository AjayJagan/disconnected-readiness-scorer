"""
Rule Assignment for DRS PR Automation

Determines which disconnected readiness rules should be applied to repositories
based on their characteristics (language, topics, name patterns, etc.).
"""



def get_rules_for_repo(name: str, language: str) -> str:
    """
    Determine appropriate DRS rules based on repository characteristics.

    Args:
        name: Repository name (e.g., "jupyter-notebook")
        language: Primary programming language (e.g., "Python", "Go", "JavaScript")

    Returns:
        Comma-separated string of rule names to apply (e.g., "csv,tags,egress,manifest,python")
    """
    # Base rules applied to all repositories
    base_rules = ["csv", "tags", "egress", "manifest"]

    # Language-specific rules
    if language and language.lower() == "python":
        base_rules.append("python")

    return ",".join(base_rules)
