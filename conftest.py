"""Make the ``custom_components`` package importable directly in tests.

The integration modules are imported by absolute name
(``custom_components.musicflow_cast...``); adding the repo root to
``sys.path`` lets pytest resolve that without involving HA's loader.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
