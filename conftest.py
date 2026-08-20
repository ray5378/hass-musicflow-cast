"""Make the integration importable directly in tests.

- repo root on sys.path  -> `from helpers import FakeHass`
- custom_components dir   -> `import musicflow_cast` / `from musicflow_cast...`
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
CC = os.path.join(HERE, "custom_components")
if CC not in sys.path:
    sys.path.insert(0, CC)
