# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Finds every fixture module in test/fixtures and lists it for pytest_plugins.

Same idea as Adafruit_Wippersnapper_Python's test/fixtures/__init__.py, but resolved from this
file's location so it works from any working directory.
"""

from pathlib import Path

_HERE = Path(__file__).resolve().parent

# "fixtures.farm", "fixtures.device", ... (files starting with _ are skipped)
ALL_FIXTURES = sorted("fixtures." + p.stem for p in _HERE.glob("[!_]*.py"))
