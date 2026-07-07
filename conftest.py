"""Make the repository root importable for tests (controller/, decider/, ...).

The project is run from its root (e.g. ``python -m interface.shell``) rather
than installed as a package, so we mirror that for pytest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
