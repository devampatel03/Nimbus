# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""T0.4 / T2.11: LoCoMo eval harness (docs/TECHNICAL_ARCHITECTURE.md's
hybrid-retrieval ablation section references "LoCoMo 85% min";
AGENT_OPERATING_MANUAL.md 3.3 P-MEMORY: "LoCoMo and LongMemEval run
locally, 3 runs, variance under 2%. Benchmark numbers are our own, not
copied from any vendor's published figures.").

Downloads the real public LoCoMo dataset (Maharana et al., "Evaluating
Very Long-Term Conversational Memory of LLM Agents") from
`snap-research/locomo`'s `data/locomo10.json` and caches it locally under
`tests/eval/locomo/data/` (already covered by the repo's `data/`
`.gitignore` pattern -- a ~2.7MB third-party file has no reason to live in
git history). If the cache exists, no network call is made.

Category mapping (`CATEGORY_NAMES`) is not documented in the dataset
itself -- derived from the LoCoMo repo's own
`task_eval/evaluation.py::eval_question_answering`, which groups
categories `[2, 3, 4]` as "single-hop, temporal, open-domain" and handles
category `1` (multi-hop, split into sub-answers) and category `5`
(adversarial, no `answer` field, expects "no information available")
separately. Cross-checked against actual question content per category
in the downloaded file (temporal -> "When did X do Y" with date answers;
open-domain -> inferential "would X..." questions; single-hop -> direct
fact lookups) to resolve which of 2/3/4 is which.

Scope, stated plainly -- this WILL show a low score, and that is the
correct, honest result to report, not a bug to work around:

- Nimbus has no real embedding model wired in (AD-7): `MemoryManager`'s
  default `DeterministicStubEmbedder` is a sha256-derived unit vector,
  deterministic but semantically meaningless. Running a semantic-
  retrieval benchmark against it measures noise, not retrieval quality.
  AD-7 said explicitly: "replace before any MEM-7 eval run" -- this is
  that eval run, confronting exactly the debt it flagged.
- Only the episodic (pgvector `memory_chunk`) tier is exercised. LoCoMo's conversation
  turns are ingested as episodic chunks; there is no entity/relation
  extraction (T2.10, not built) to also populate the semantic graph tier,
  so `MemoryQuery.active_entities` is never populated and
  `MemoryManager._query_semantic` always short-circuits to `[]` (AD-9).
- Scoring is retrieval recall, not QA accuracy: Nimbus's orchestrator has
  no generation model (T1.5/T4.6 outstanding) to synthesize a final
  answer from retrieved chunks, so "correct" means the gold answer's
  content is recoverable from the top-k retrieved episodic text (token
  overlap >= `RECALL_THRESHOLD`), not that a generated answer matches.
- Category 5 (adversarial -- no `answer` field, expects "no information
  available") is excluded from the accuracy figure for the same reason:
  there is nothing to check retrieved content against, and no generator
  to judge whether it would have correctly declined to answer. Its count
  is still reported for transparency. This matches the task's requested
  breakdown of exactly single-hop/multi-hop/open-domain/temporal.
- Each of the 10 LoCoMo samples is a separate conversation between a
  different pair of speakers. `memory_chunk` is append-only (DELETE/TRUNCATE
  are blocked by trigger, R7), so isolation is by a fresh scratch database
  per sample (`nimbus_locomo_<random>`, schema from `Base.metadata.create_all`,
  same DDL/triggers as the migration), dropped right after the sample. The
  dev `nimbus` database is never touched. (H14: ported from Qdrant in P1.3b;
  scoring, dataset, categories and thresholds are unchanged.)
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from config.settings import settings
from src.contracts.event import Event, EventKind
from src.contracts.memory_query import MemoryQuery
from src.memory.embedder import BatchEmbedder
from src.memory.manager import ChunkIn, MemoryManager
from src.memory.schemas import Base

DATASET_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
DEFAULT_CACHE_PATH = Path(__file__).parent / "data" / "locomo10.json"
DATE_FORMAT = "%I:%M %p on %d %B, %Y"
RECALL_THRESHOLD = 0.5
TARGET_MIN_ACCURACY = 0.85
_NIL_USER_ID = uuid.UUID(int=0)

CATEGORY_NAMES: dict[int, str] = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}
SCORED_CATEGORIES = (1, 2, 3, 4)


def load_dataset(cache_path: Path = DEFAULT_CACHE_PATH, url: str = DATASET_URL) -> list[dict]:
    if not cache_path.exists():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        response = httpx.get(url, timeout=60.0, follow_redirects=True)
        response.raise_for_status()
        cache_path.write_bytes(response.content)
    return json.loads(cache_path.read_text(encoding="utf-8"))


def _normalize_tokens(text: str) -> set[str]:
    return {tok.strip(".,!?;:'\"()").lower() for tok in str(text).split() if len(tok) > 2}


def _recall(answer: str, retrieved_text: str) -> float:
    answer_tokens = _normalize_tokens(answer)
    if not answer_tokens:
        return 0.0
    retrieved_tokens = _normalize_tokens(retrieved_text)
    hits = sum(1 for tok in answer_tokens if tok in retrieved_tokens)
    return hits / len(answer_tokens)


def _iter_turns(conversation: dict) -> list[tuple[datetime, str, str]]:
    """Yields (occurred_at, speaker, text) for every turn across every
    `session_N` key, in session order.
    """
    turns: list[tuple[datetime, str, str]] = []
    session_index = 1
    while f"session_{session_index}" in conversation:
        date_key = f"session_{session_index}_date_time"
        occurred_at = datetime.strptime(conversation[date_key], DATE_FORMAT).replace(tzinfo=UTC)
        for turn in conversation[f"session_{session_index}"]:
            turns.append((occurred_at, turn["speaker"], turn["text"]))
        session_index += 1
    return turns


def _scratch_dsn(db: str) -> str:
    return settings.postgres_dsn.rsplit("/", 1)[0] + f"/{db}"


async def _admin(sql: str) -> None:
    engine = create_async_engine(
        _scratch_dsn("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def _create_scratch_engine() -> tuple[str, AsyncEngine]:
    name = f"nimbus_locomo_{uuid.uuid4().hex[:10]}"
    await _admin(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(_scratch_dsn(name), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    return name, engine


async def _drop_scratch(name: str, engine: AsyncEngine) -> None:
    await engine.dispose()
    await _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def _run_sample(manager: MemoryManager, sample: dict, top_k: int) -> dict[str, dict]:
    # P2.4: the store takes the caller's real event. One event per LoCoMo
    # session (the provenance root, like a Recorder transcript); each turn is a
    # chunk at its position, so the write is one embedding batch per session.
    turns = _iter_turns(sample["conversation"])
    for n, (occurred_at, group) in enumerate(groupby(turns, key=lambda t: t[0])):
        event_id = await manager.record_event(
            Event(
                source_key=f"locomo:{sample['sample_id']}:{n}",
                kind=EventKind.TEXT,
                source="locomo",
                lane="trusted",
                occurred_at=occurred_at,
            )
        )
        await manager.write_episodic(
            event_id, [ChunkIn(text=f"{speaker}: {text}") for _, speaker, text in group]
        )

    per_category: dict[str, dict] = defaultdict(lambda: {"correct": 0, "total": 0})
    for qa in sample["qa"]:
        category = qa["category"]
        name = CATEGORY_NAMES.get(category, f"unknown_{category}")
        if category not in SCORED_CATEGORIES:
            per_category[name]["total"] += 1  # counted, not scored -- see module docstring
            continue

        query_context = MemoryQuery(user_id=_NIL_USER_ID, query_text=qa["question"], top_k=top_k)
        results = await manager.query(qa["question"], query_context)
        retrieved_text = " ".join(r.content for r in results)
        recall = _recall(qa["answer"], retrieved_text)

        per_category[name]["total"] += 1
        if recall >= RECALL_THRESHOLD:
            per_category[name]["correct"] += 1

    return per_category


async def run(
    max_samples: int | None = None, top_k: int = 5, embedder: BatchEmbedder | None = None
) -> dict[str, object]:
    dataset = load_dataset()
    if max_samples is not None:
        dataset = dataset[:max_samples]

    totals: dict[str, dict] = defaultdict(lambda: {"correct": 0, "total": 0})
    for sample in dataset:
        name, engine = await _create_scratch_engine()
        try:
            async with engine.connect() as conn:
                pg_session = AsyncSession(bind=conn, expire_on_commit=False)
                manager = MemoryManager(pg_session=pg_session, embedder=embedder)
                per_category = await _run_sample(manager, sample, top_k)
                await pg_session.close()
            for cat, stats in per_category.items():
                totals[cat]["correct"] += stats["correct"]
                totals[cat]["total"] += stats["total"]
        finally:
            await _drop_scratch(name, engine)

    scored_correct = sum(totals[n]["correct"] for n in totals if n != "adversarial")
    scored_total = sum(totals[n]["total"] for n in totals if n != "adversarial")
    overall = scored_correct / scored_total if scored_total else 0.0

    return {
        "samples": len(dataset),
        "per_category": dict(totals),
        "overall_accuracy": overall,
        "target_min": TARGET_MIN_ACCURACY,
        "passed": overall >= TARGET_MIN_ACCURACY,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="LoCoMo retrieval-recall eval harness")
    parser.add_argument("--max-samples", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    result = asyncio.run(run(max_samples=args.max_samples, top_k=args.top_k))

    print(f"[locomo] {result['samples']} sample(s) evaluated")
    for name, stats in sorted(result["per_category"].items()):
        rate = stats["correct"] / stats["total"] if stats["total"] else 0.0
        scored_note = "" if name != "adversarial" else " (not scored -- see module docstring)"
        print(f"  {name}: {stats['correct']}/{stats['total']} ({rate:.1%}){scored_note}")
    print(
        f"[locomo] overall accuracy (excl. adversarial): {result['overall_accuracy']:.1%}, "
        f"target >= {result['target_min']:.0%}"
    )
    print("PASS" if result["passed"] else "FAIL")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
