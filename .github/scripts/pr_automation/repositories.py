#!/usr/bin/env python3
"""
Repository Operations for DRS PR Automation

All repository-related functionality including classification and metadata operations.
"""

from typing import Set, List, Dict, Any
from dataclasses import dataclass

from .workflows import WorkflowDetector
from .utils import READY_FOR_WORKFLOW, ALREADY_HAS_WORKFLOW, ARCHIVED_REPOSITORY, EXCLUDED_REPOSITORY, API_ERROR


@dataclass
class RepositoryClassification:
    """Result of repository classification"""
    repo_name: str
    should_process: bool
    category: str
    reason: str
    details: Dict[str, Any]


class RepositoryClassifier:
    """Classifies repositories for processing operations."""

    def __init__(self, exclusions: Set[str], workflow_detector: WorkflowDetector, force_update: bool = False):
        self.exclusions = exclusions
        self.workflow_detector = workflow_detector
        self.force_update = force_update
        self.categories = {
            READY_FOR_WORKFLOW: 'Ready for Processing',
            ALREADY_HAS_WORKFLOW: 'Already Has Workflow',
            EXCLUDED_REPOSITORY: 'Excluded Repository',
            ARCHIVED_REPOSITORY: 'Archived Repository',
            API_ERROR: 'API Error'
        }

    def _is_excluded_repository(self, repo) -> bool:
        """Check if repository is in the exclusion list."""
        return repo.full_name in self.exclusions

    def _categorize_reason(self, reason: str) -> str:
        """Map processing reason to a category for reporting."""
        if reason.startswith(ALREADY_HAS_WORKFLOW):
            return ALREADY_HAS_WORKFLOW
        elif reason == ARCHIVED_REPOSITORY:
            return ARCHIVED_REPOSITORY
        elif reason == READY_FOR_WORKFLOW:
            return READY_FOR_WORKFLOW
        else:
            return API_ERROR

    def classify_repository(self, repo, trigger_reason: str = "manual") -> RepositoryClassification:
        """Classify a single repository for processing."""
        try:
            # Check exclusion list first
            if self._is_excluded_repository(repo):
                return RepositoryClassification(
                    repo_name=repo.full_name,
                    should_process=False,
                    category=EXCLUDED_REPOSITORY,
                    reason=f"Repository in exclusion list",
                    details={'excluded': True}
                )

            # Use workflow detector's decision logic
            should_process, reason = self.workflow_detector.should_process_repository(repo)

            # Override: force update for template changes or when specific repos are targeted
            if reason.startswith(ALREADY_HAS_WORKFLOW) and (trigger_reason == 'template_change' or self.force_update):
                should_process = True
                reason = f"Forcing update for existing workflow (trigger: {trigger_reason})"
                category = READY_FOR_WORKFLOW
            else:
                # Determine category based on reason
                category = self._categorize_reason(reason)

            return RepositoryClassification(
                repo_name=repo.full_name,
                should_process=should_process,
                category=category,
                reason=reason,
                details={}
            )

        except Exception as e:
            return RepositoryClassification(
                repo_name=getattr(repo, 'full_name', 'unknown'),
                should_process=False,
                category=API_ERROR,
                reason=f"Classification error: {str(e)}",
                details={'error': str(e)}
            )

    def classify_repositories(self, repositories, trigger_reason: str = "manual") -> Dict[str, List[RepositoryClassification]]:
        """Classify multiple repositories and group by category."""
        classifications = []

        for repo in repositories:
            try:
                classification = self.classify_repository(repo, trigger_reason)
                classifications.append(classification)
            except Exception as e:
                print(f"Error classifying {getattr(repo, 'full_name', 'unknown')}: {e}")

        # Group by category
        grouped = {}
        for classification in classifications:
            category = classification.category
            if category not in grouped:
                grouped[category] = []
            grouped[category].append(classification)

        return grouped

    def generate_summary_report(self, grouped_classifications: Dict[str, List[RepositoryClassification]]) -> str:
        """Generate a human-readable summary report of classifications."""
        total_repos = sum(len(repos) for repos in grouped_classifications.values() if isinstance(repos, list))

        report_lines = [
            "REPOSITORY PROCESSING ANALYSIS",
            "=" * 50,
            f"Total repositories analyzed: {total_repos}",
            ""
        ]

        # Ready for processing
        ready = grouped_classifications.get(READY_FOR_WORKFLOW, [])
        if ready:
            report_lines.extend([
                f"Ready for processing ({len(ready)}):",
                *[f"   - {repo.repo_name}" for repo in ready[:10]],
                *(["   - ... and more"] if len(ready) > 10 else []),
                ""
            ])

        # Already has workflow
        existing = grouped_classifications.get(ALREADY_HAS_WORKFLOW, [])
        if existing:
            report_lines.extend([
                f"Already has workflow ({len(existing)}):",
                *[f"   - {repo.repo_name}" for repo in existing[:5]],
                *(["   - ... and more"] if len(existing) > 5 else []),
                ""
            ])

        # Skipped categories
        skip_categories = [
            (EXCLUDED_REPOSITORY, 'Excluded repositories'),
            (ARCHIVED_REPOSITORY, 'Archived'),
            ('incompatible', 'Incompatible'),
            ('fork_external', 'External forks'),
            ('test_repository', 'Test repositories'),
        ]

        skipped_total = 0
        for category_key, category_name in skip_categories:
            repos = grouped_classifications.get(category_key, [])
            if repos:
                skipped_total += len(repos)
                report_lines.extend([
                    f"{category_name} ({len(repos)}):",
                    *[f"   - {repo.repo_name}" for repo in repos[:3]],
                    *(["   - ... and more"] if len(repos) > 3 else []),
                    ""
                ])

        # Summary
        processed = len(ready)
        existing_count = len(existing)

        report_lines.extend([
            "=" * 50,
            "SUMMARY:",
            f"  Ready to process: {processed}",
            f"  Already processed: {existing_count}",
            f"  Skipped: {skipped_total}",
            f"  Success rate: {((processed + existing_count) / total_repos * 100):.1f}%" if total_repos > 0 else "  Success rate: 0%"
        ])

        return "\n".join(report_lines)

