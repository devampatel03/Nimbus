# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Process wiring: one process, three supervised loops (TARGET_DESIGN section 9,
ADR-0008, ADR-0012).

* gateway loop   -- gateway -> ingest (`Ingestor.run`) -> turn -> reply
* worker loop    -- `Ingestor.recover_pending()` periodically (it applies its own
                    retry backoff) and the `JobRunner` seam (P3.3)
* scheduler loop -- the `Scheduler` seam (P3.3)

Startup order is a security property, so it is fixed:

1. `install_log_scrubbing(secrets)` -- before anything else can log (R12, INV-7).
2. Character card through `src.app.card.load_card` only; refuse to start if it
   is not approved/pinned (R9). The operator dev switch is honoured only with
   `APP_ENV=development` (see `src/app/card.py`).
3. Database engine (parameters hidden, statement timeout), then the gateway and
   the single `GuardedLLM`/`CallAccountant` (R4, R5).
4. `MemoryManager.verify_index()`: a model/dim change aborts startup.
5. Loops start. Policy is `DenyAllWritesPolicy` until P3.1 (INV-5).

The Telegram token is read by `GatewaySettings`; no value is ever logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from config.settings import Settings as DbSettings
from src.app.card import StartupRefused, load_card
from src.app.db import build_engine, build_session_factory
from src.app.heartbeat import Heartbeat, HeartbeatPathError
from src.app.pipeline import PendingTurn, TurnGateway, TurnWorker
from src.app.recall import SessionScopedRecall
from src.app.seams import JobRunner, NoopJobRunner, NoopScheduler, Scheduler
from src.app.settings import AppSettings
from src.app.supervisor import EXIT_FATAL_LOOP, LoopSpec, Supervisor
from src.core.logging import install_log_scrubbing
from src.db.crypto import KEY_ENV_VAR, DataCipher
from src.gateway.protocols import Gateway
from src.gateway.settings import GatewaySettings
from src.gateway.store import UpdateStore
from src.gateway.telegram import TelegramGateway
from src.gateway.types import TelegramAuthError
from src.ingest import EventSink, Ingestor, PgEventSink, Transcriber, UnavailableTranscriber
from src.llm import GuardedLLM, build_guarded_llm
from src.llm.factory import build_api_embedder
from src.llm.pg_stores import PostgresAuditStore, PostgresFlagStore
from src.llm.protocols import Provider
from src.llm.settings import DEFAULT_API_KEY, LLMSettings
from src.memory.embedder import (
    BatchEmbedder,
    EmbeddingMigrationRequired,
    Qwen3Embedder,
)
from src.memory.manager import MemoryManager
from src.memory.store import VectorEmbedder
from src.net import EgressClient, NetSettings
from src.orchestrator.dispatch import DenyAllWritesPolicy
from src.orchestrator.loop import Orchestrator
from src.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_MIGRATION_REQUIRED = 3

SessionFactory = async_sessionmaker[AsyncSession]


class EmbedderLike(BatchEmbedder, VectorEmbedder, Protocol):
    """Both the batch protocol (index check) and the store protocol (search)."""


@dataclass(frozen=True)
class AppConfig:
    app: AppSettings
    llm: LLMSettings
    net: NetSettings
    gateway: GatewaySettings | None
    dsn: str = field(repr=False)  # carries the DB password
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls) -> AppConfig:
        app = AppSettings()
        gateway = None if app.fake_backends else GatewaySettings()  # type: ignore[call-arg]
        return cls.build(app=app, gateway=gateway, llm=LLMSettings(), net=NetSettings())

    @classmethod
    def build(
        cls,
        *,
        app: AppSettings,
        gateway: GatewaySettings | None,
        llm: LLMSettings,
        net: NetSettings,
        db: DbSettings | None = None,
        dsn: str | None = None,
    ) -> AppConfig:
        if dsn is None:
            from config.settings import settings as shared

            db = db or shared
            dsn = db.postgres_dsn
        secrets: list[str] = []
        if gateway is not None:
            secrets.append(gateway.bot_token.get_secret_value())
        key = llm.api_key.get_secret_value()
        if key != DEFAULT_API_KEY:
            secrets.append(key)
        secrets.extend(llm.canaries)
        if db is not None:
            secrets.append(db.postgres_password.get_secret_value())
        data_key = os.environ.get(KEY_ENV_VAR)
        if data_key:
            secrets.append(data_key)
        return cls(app, llm, net, gateway, dsn, tuple(dict.fromkeys(secrets)))


@dataclass
class Backends:
    """The network-facing pieces. Real in production, in-process fakes in tests
    and under `APP_FAKE_BACKENDS`."""

    gateway: Gateway
    provider: Provider | None = None  # None => the real OpenAI-compatible provider
    embedder: EmbedderLike | None = None  # None => per APP_EMBEDDER
    transcriber: Transcriber = field(default_factory=UnavailableTranscriber)
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


Beat = Callable[[], None]
# (config, sessions, gateway liveness beat) -> backends. The gateway must call
# `beat` after every completed poll cycle; that is the gateway loop's heartbeat.
BackendFactory = Callable[[AppConfig, SessionFactory, Beat], Backends]


def real_backends(cfg: AppConfig, sf: SessionFactory, beat: Beat) -> Backends:
    if cfg.gateway is None:
        raise StartupRefused("telegram settings are required")
    egress = EgressClient(cfg.net)
    gw = TelegramGateway(cfg.gateway, egress, UpdateStore(sf), on_poll=beat)
    return Backends(gateway=gw, closers=[gw.aclose, egress.aclose])


def fake_backends(cfg: AppConfig, sf: SessionFactory, beat: Beat) -> Backends:
    from src.app.fakes import InMemoryGateway
    from src.llm.fake import FakeLLM
    from src.memory.manager import DeterministicStubEmbedder

    gw = InMemoryGateway(on_poll=beat)
    return Backends(
        gateway=gw,
        provider=FakeLLM(default="fake reply"),
        embedder=DeterministicStubEmbedder(),
        closers=[gw.aclose],
    )


async def verify_memory_index(sf: SessionFactory, embedder: BatchEmbedder) -> None:
    """Refuse to run against an index built by another model/dim. Only an
    unreachable database is tolerated (reported by type name; the loops will
    report it again and the heartbeat goes stale)."""
    try:
        async with sf() as session:
            await MemoryManager(pg_session=session, embedder=embedder).verify_index()
    except EmbeddingMigrationRequired:
        raise
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        log.warning("memory index check skipped (database unavailable) err=%s", type(exc).__name__)


def build_embedder(cfg: AppConfig, llm: GuardedLLM) -> EmbedderLike:
    if cfg.app.embedder == "api":
        # Shares GuardedLLM's CallAccountant: one cap, one set of held spend (R5).
        return build_api_embedder(llm, cfg.llm)
    return Qwen3Embedder()


async def _first_done(*coros: Coroutine[Any, Any, None]) -> None:
    """Run side by side; when any ends, cancel the rest and re-raise its error."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.result()  # raises the first failure unwrapped (type name stays useful)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Runtime:
    """The assembled process. `loops()` is what the supervisor runs."""

    def __init__(
        self,
        *,
        cfg: AppConfig,
        gateway: Gateway,
        sink: EventSink,
        transcriber: Transcriber,
        orchestrator: Orchestrator,
        heartbeat: Heartbeat,
        stop: asyncio.Event,
        jobs: JobRunner | None = None,
        scheduler: Scheduler | None = None,
    ) -> None:
        self.cfg = cfg
        self.heartbeat = heartbeat
        self.stop = stop
        self._jobs: JobRunner = jobs or NoopJobRunner()
        self._scheduler: Scheduler = scheduler or NoopScheduler()
        self.queue: asyncio.Queue[PendingTurn] = asyncio.Queue(cfg.app.turn_queue_size)
        # Ingest reads through the wrapper, so every ack is followed by a queued turn.
        self.gateway = TurnGateway(gateway, self.queue)
        self.ingestor = Ingestor(sink, self.gateway, transcriber)
        self.turns = TurnWorker(
            self.gateway,
            orchestrator,
            self.queue,
            history_messages=cfg.app.history_messages,
            on_progress=lambda: heartbeat.beat("gateway"),
        )
        self._beat_every = min(15.0, cfg.app.heartbeat_stale_s / 3)

    async def _idle(self, name: str, seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and not self.stop.is_set():
            step = min(remaining, self._beat_every)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=step)
            self.heartbeat.beat(name)
            remaining -= step

    async def gateway_loop(self) -> None:
        # Bound-method references, not `.run(` calls: `Ingestor.run` is not a tool
        # execution, and the INV-5 call-site pin must stay free of exceptions.
        consume = self.ingestor.run
        await _first_done(consume(), self.turns.serve())

    async def worker_loop(self) -> None:
        while not self.stop.is_set():
            await self.ingestor.recover_pending(self.cfg.app.recover_limit)
            await self._jobs.run_due()
            self.heartbeat.beat("worker")
            await self._idle("worker", self.cfg.app.recover_interval_s)

    async def scheduler_loop(self) -> None:
        while not self.stop.is_set():
            await self._scheduler.tick()
            self.heartbeat.beat("scheduler")
            await self._idle("scheduler", self.cfg.app.scheduler_tick_s)

    def loops(self) -> list[LoopSpec]:
        return [
            LoopSpec("gateway", self.gateway_loop, fatal=(TelegramAuthError,)),
            LoopSpec("worker", self.worker_loop),
            LoopSpec("scheduler", self.scheduler_loop),
        ]


def _install_signal_handlers(stop: asyncio.Event) -> Callable[[], None]:
    """SIGINT/SIGTERM set `stop`. Where the loop cannot register handlers
    (Windows) fall back to `signal.signal`; `restore` puts back whatever was
    installed before, on both paths."""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    previous: dict[signal.Signals, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            with contextlib.suppress(ValueError, OSError):
                previous[sig] = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    def restore() -> None:
        for sig in installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, handler)

    return restore


async def run_app(
    cfg: AppConfig,
    *,
    backends: BackendFactory | None = None,
    stop: asyncio.Event | None = None,
    engine: AsyncEngine | None = None,
    handle_signals: bool = False,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> int:
    # 1. Scrubbing first: nothing below may log before this line.
    install_log_scrubbing(cfg.secrets)
    logging.basicConfig(level=cfg.app.log_level)
    stop = stop or asyncio.Event()

    if cfg.app.fake_backends and cfg.app.env != "development":
        log.critical("APP_FAKE_BACKENDS is only allowed with APP_ENV=development; refusing")
        return EXIT_CONFIG

    # 2. Character card (R9), fail closed.
    try:
        card = load_card(cfg.app.env)
    except Exception as exc:  # noqa: BLE001 - CardNotApprovedError, StartupRefused, OSError
        log.critical("startup refused: character card not usable err=%s", type(exc).__name__)
        return EXIT_CONFIG

    heartbeat = Heartbeat(cfg.app.heartbeat_path)
    try:
        heartbeat.prepare()  # private dir (0700); refuse a shared/foreign location
    except HeartbeatPathError as exc:
        log.critical("startup refused: %s", exc)
        return EXIT_CONFIG

    own_engine = engine is None
    eng = engine or build_engine(cfg.dsn, statement_timeout_s=cfg.app.db_statement_timeout_s)
    sf = build_session_factory(eng)
    be: Backends | None = None
    restore_signals: Callable[[], None] | None = None
    try:
        # 3. Backends and the one GuardedLLM.
        factory = backends or (fake_backends if cfg.app.fake_backends else real_backends)
        be = factory(cfg, sf, lambda: heartbeat.beat("gateway"))
        cipher = DataCipher.from_env() if cfg.llm.store_payload else None
        audit = PostgresAuditStore(sf, cipher=cipher, retention_days=cfg.llm.payload_retention_days)
        llm = build_guarded_llm(
            cfg.llm,
            provider=be.provider,
            flags=PostgresFlagStore(sf),
            audit=audit,
            ledger=audit,
        )
        embedder = be.embedder or build_embedder(cfg, llm)

        # 4. Index compatibility.
        try:
            await verify_memory_index(sf, embedder)
        except EmbeddingMigrationRequired:
            log.critical("startup refused: embedding model changed, migration required")
            return EXIT_MIGRATION_REQUIRED

        # 5. Assemble and run.
        orchestrator = Orchestrator(
            llm,
            ToolRegistry().loop_tools(),
            DenyAllWritesPolicy(),
            character_card=card,
            recall=SessionScopedRecall(sf, embedder),
        )
        rt = Runtime(
            cfg=cfg,
            gateway=be.gateway,
            sink=PgEventSink(sf),
            transcriber=be.transcriber,
            orchestrator=orchestrator,
            heartbeat=heartbeat,
            stop=stop,
        )
        specs = rt.loops()
        heartbeat.register(*(s.name for s in specs))
        if handle_signals:
            restore_signals = _install_signal_handlers(stop)
        log.info("nimbus started env=%s loops=%d", cfg.app.env, len(specs))
        sup = Supervisor(
            specs,
            stop=stop,
            backoff_initial_s=cfg.app.backoff_initial_s,
            backoff_max_s=cfg.app.backoff_max_s,
            reset_after_s=cfg.app.backoff_reset_after_s,
            grace_s=cfg.app.shutdown_grace_s,
            sleep=sleep,
        )
        code = await sup.serve()
        pending = rt.queue.qsize()
        if pending:
            log.warning("shutdown with %d unanswered turn(s) queued", pending)
        log.info("nimbus stopped code=%d", code)
        return EXIT_FATAL_LOOP if code == EXIT_FATAL_LOOP else EXIT_OK
    finally:
        if restore_signals is not None:
            restore_signals()
        if be is not None:
            for close in be.closers:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 - shutdown must finish
                    log.warning("close failed err=%s", type(exc).__name__)
        if own_engine:
            await eng.dispose()
        heartbeat.clear()


def describe_validation_error(exc: ValidationError) -> str:
    """Field names only; `hide_input_in_errors` already keeps values out, this
    keeps messages out too."""
    fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
    return "invalid configuration: " + ", ".join(fields)
