# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-4: Mode A response path never blocks on a network call.

Acceptance: driving the Mode A response path with all network calls
stubbed to hang indefinitely must still return within the local
processing budget — i.e. no network call sits on the critical path.

Per this session's task: left as a stub (`pytest.skip`, not a raw
ModuleNotFoundError) because `src/orchestrator/mode_a.py` does not exist
yet — no P-AGENT work has landed for Mode A. Once `handle_turn` exists,
this becomes a real assertion patching httpx to hang and asserting
`handle_turn` still returns inside the Mode A latency budget
(docs/TECHNICAL_ARCHITECTURE.md section 4).
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

MODE_A_MODULE = Path(__file__).resolve().parents[3] / "src" / "orchestrator" / "mode_a.py"
MODE_A_LATENCY_BUDGET_S = 1.0


@pytest.mark.skipif(
    not MODE_A_MODULE.exists(),
    reason="INV-4 stub: src/orchestrator/mode_a.py (handle_turn) not implemented yet",
)
@pytest.mark.asyncio
async def test_mode_a_response_does_not_block_on_network(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("mode_a", MODE_A_MODULE)
    assert spec is not None and spec.loader is not None
    mode_a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mode_a)

    async def hang_forever(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr("httpx.AsyncClient.post", hang_forever, raising=False)
    monkeypatch.setattr("httpx.AsyncClient.get", hang_forever, raising=False)

    await asyncio.wait_for(mode_a.handle_turn(), timeout=MODE_A_LATENCY_BUDGET_S)
