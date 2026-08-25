#!/usr/bin/env python3
"""Run unittest discovery with a deterministic random test order."""

import argparse
import os
import random
import sys
import unittest

# Match `python3 -m unittest discover` semantics: -m puts the CWD (repo root)
# on sys.path so `from tools import ...` resolves; a script puts only its own
# directory there. Insert the CWD explicitly or the idempotency module fails
# to import and its ~90 tests silently vanish from the shuffled run.
sys.path.insert(0, os.getcwd())


def leaves(suite):
    for member in suite:
        if isinstance(member, unittest.TestSuite):
            yield from leaves(member)
        else:
            yield member


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--start-directory", default="tools/tests")
    args = parser.parse_args()
    tests = list(leaves(unittest.defaultTestLoader.discover(args.start_directory)))
    random.Random(args.seed).shuffle(tests)
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(tests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
