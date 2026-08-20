"""Root test conftest to ensure runtime package can be imported."""

import pathlib
import sys

# Ensure the project root is in sys.path at the front BEFORE any runtime imports
project_root = pathlib.Path(__file__).parent.parent
if str(project_root) in sys.path:
    sys.path.remove(str(project_root))
sys.path.insert(0, str(project_root))
