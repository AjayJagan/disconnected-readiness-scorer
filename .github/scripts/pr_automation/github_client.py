#!/usr/bin/env python3
"""
GitHub Client for DRS PR Automation

Wraps GitHub API operations and utilities including rate limiting.
"""

import os
from typing import Dict, Any, Tuple
from dataclasses import dataclass
from github import Github

from .utils import retry_github_operation, RATE_LIMIT_SAFETY_THRESHOLD


@dataclass
class RateLimitStatus:
    """GitHub API rate limit status"""
    is_safe: bool
    remaining: int
    total: int
    percentage_used: float
    status_message: str


class GitHubClient:
    """Wraps GitHub API operations and utilities."""

    def __init__(self):
        self.client = self._create_client()

    def _create_client(self) -> Github:
        """Create GitHub client with proper authentication."""
        token = os.getenv('GH_APP_DR_TOKEN')

        if not token:
            raise ValueError(
                "Missing required GitHub App token.\n"
                "Set GH_APP_DR_TOKEN secret with a GitHub App installation token that has:\n"
                "  - Contents: Write\n"
                "  - Pull requests: Write\n"
                "  - Metadata: Read\n"
                "  - Installed on target organizations (opendatahub-io)"
            )

        return Github(token)

    def check_rate_limit(self) -> RateLimitStatus:
        """Check GitHub API rate limit status."""
        try:
            rate_limit = self.client.get_rate_limit()
            remaining = rate_limit.core.remaining
            total = rate_limit.core.limit

            is_safe = remaining >= RATE_LIMIT_SAFETY_THRESHOLD
            percentage_used = ((total - remaining) / total) * 100

            status_message = f"{remaining}/{total} requests remaining ({percentage_used:.1f}% used)"

            return RateLimitStatus(
                is_safe=is_safe,
                remaining=remaining,
                total=total,
                percentage_used=percentage_used,
                status_message=status_message
            )

        except (OSError, IOError, ValueError, AttributeError) as e:
            return RateLimitStatus(
                is_safe=False,
                remaining=0,
                total=0,
                percentage_used=100.0,
                status_message=f"Error checking rate limit: {str(e)}"
            )

    def get_repo_metadata(self, repo) -> Dict[str, Any]:
        """Extract minimal metadata needed for simplified rule assignment."""
        return {
            'name': repo.name,  # Still needed for logging/display
            'language': repo.language,  # Check if Python (for python rule)
        }

    def get_account(self, org_name: str) -> Tuple[Any, str, int]:
        """Get organization or user account with retry logic."""
        def _get_account():
            try:
                # Try organization first
                account = self.client.get_organization(org_name)
                return account, "organization", account.public_repos
            except Exception as org_error:
                # If organization lookup fails, try user
                try:
                    account = self.client.get_user(org_name)
                    return account, "user", account.public_repos
                except Exception:
                    # If both fail, raise a combined error message
                    raise Exception(f"Could not find organization or user '{org_name}'. Original org error: {org_error}")

        return retry_github_operation(_get_account)