"""Allow `python -m cellar`."""

import sys

from .cli import main

sys.exit(main())
