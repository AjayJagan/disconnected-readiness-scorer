#!/usr/bin/env python3
"""
Entry point for running pr_automation as a module.

This allows: python -m pr_automation
Instead of: python -m pr_automation.main
"""

from .main import main

if __name__ == '__main__':
    main()