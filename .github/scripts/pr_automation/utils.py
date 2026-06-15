#!/usr/bin/env python3
"""
Utilities for DRS PR Automation

Common utilities and constants used across the automation system.
"""

import time

from github import UnknownObjectException


# Configuration constants
RATE_LIMIT_SAFETY_THRESHOLD = 100  # Minimum API requests before warning
RATE_LIMIT_CHECK_INTERVAL = 5      # Check rate limit every N successful operations
MAX_RETRIES = 3                    # Maximum retry attempts for GitHub API calls
RETRY_BASE_DELAY = 1               # Base delay between retries (seconds)

# State management operation constants
OP_CHECK = 'check'
OP_MARK_CHECKED = 'mark_checked'
OP_CHECK_CHANGED = 'check_changed'
OP_UPDATE_STATE = 'update_state'

# State management message constants
MSG_TEMPLATE_UNCHANGED = "Template unchanged"
MSG_TEMPLATE_NOT_FOUND = "Template file not found"
MSG_INITIAL_TEMPLATE_SETUP = "Initial template tracking setup"

# Repository classification constants
READY_FOR_WORKFLOW = 'ready_for_workflow'
ALREADY_HAS_WORKFLOW = 'already_has_workflow'
ARCHIVED_REPOSITORY = 'archived_repository'
EXCLUDED_REPOSITORY = 'excluded_repository'
API_ERROR = 'api_error'


def retry_github_operation(func, max_retries=MAX_RETRIES):
    """Simple retry wrapper for GitHub API operations."""
    for attempt in range(max_retries):
        try:
            return func()
        except (KeyboardInterrupt, SystemExit):
            # Don't retry on cancellation signals
            raise
        except Exception as e:
            if attempt == max_retries - 1:  # Last attempt
                raise e

            # Calculate exponential backoff delay
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            print(f"    Retry {attempt + 1}/{max_retries} in {delay}s after error: {str(e)[:100]}")
            time.sleep(delay)

    # This should never be reached, but just in case
    raise Exception("Max retries exceeded")


def file_exists_in_repo(repo, file_path: str) -> bool:
    """Check if a file exists in the repository.

    Args:
        repo: GitHub repository object
        file_path: Path to the file to check (e.g., '.disconnected-readiness/config.yaml')

    Returns:
        bool: True if file exists, False otherwise
    """
    try:
        repo.get_contents(file_path)
        return True
    except UnknownObjectException:
        return False
