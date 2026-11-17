"""backend/src holds flat modules (import config, import connectors), not a
package, so put it on sys.path for the tests. Run from backend/:
uv run pytest"""

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
