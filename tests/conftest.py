import sys
from pathlib import Path

# The project is a flat set of top-level modules rather than a package, so
# tests import them the same way checker.py does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
