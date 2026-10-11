"""Loads the read-only tap / SEP-2028 prototype hooks, then runs the unchanged server file (R-8)."""
import runpy, sys

import oa_hooks  # noqa: F401

runpy.run_path(sys.argv[1], run_name="__main__")
