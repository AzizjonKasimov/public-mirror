# /// script
# requires-python = ">=3.11"
# dependencies = ["pypdf>=4"]
# ///
"""Run public-mirror without installing it.

    uv run path/to/public-mirror.py check     # with PDF text scanning
    python path/to/public-mirror.py check     # without it, unless pypdf is installed
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from public_mirror.cli import main  # noqa: E402

sys.exit(main())
