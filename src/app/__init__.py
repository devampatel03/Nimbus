# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Nimbus app entrypoint (P2.1b): one process, three supervised asyncio loops
(gateway, worker, scheduler). Run with `python -m src.app`; probe with
`python -m src.app healthcheck`."""
