#!/usr/bin/env python3
"""Management entry point for the development/test host project."""

import os
import sys

from django.core.management import execute_from_command_line


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings")
    execute_from_command_line(sys.argv)
