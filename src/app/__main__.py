# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""`python -m src.app` runs the bot; `python -m src.app healthcheck` probes it.

Exit codes: 0 ok / clean stop, 1 unhealthy (healthcheck), 2 configuration or
startup refused (bad settings, unapproved character card, any other startup
failure), 3 embedding migration
required, 4 a loop failed fatally (for example a revoked bot token).
"""

from __future__ import annotations

import sys

from src.app.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
