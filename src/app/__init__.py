# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Nimbus app entrypoint (P2.1b): one process, three supervised asyncio loops
(gateway, worker, scheduler). Run with `python -m src.app`; probe with
`python -m src.app healthcheck`."""
