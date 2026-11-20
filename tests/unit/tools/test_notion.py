# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P3.7: Notion tools. Page text reaches the model only via ToolOutput.external;
ids only as opaque handles; the only write is an approved append."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest

from src.approvals.types import AskOutcome, AuditRecord
from src.core.turn_context import turn_event
from src.lanes.data import UntrustedText
from src.llm.fake import FakeLLM
from src.orchestrator.dispatch import MAX_EXTERNAL_ITEMS, Dispatcher, Risk, ToolError
from src.orchestrator.loop import Orchestrator
from src.policy import (
    LOCAL_SCOPES,
    TOOL_RULES,
    PolicyContext,
    PolicyEngine,
    PolicyMode,
    ProposedCall,
)
from src.tools.integrations.notion import HANDLE_RE, MAX_EXTERNAL_CHARS, HandleBook, NotionTool
from tests.fakes.llm import build_env
from tests.unit.integrations.notion.notion_helpers import P1, P2, FakeNotion, fx, ok

SEARCH = ("POST", "/v1/search")
PAGE1 = ("GET", f"/v1/pages/{P1}")
PAGE2 = ("GET", f"/v1/pages/{P2}")
CH1 = ("GET", f"/v1/blocks/{P1}/children")
CH2 = ("GET", f"/v1/blocks/{P2}/children")
PATCH1 = ("PATCH", f"/v1/blocks/{P1}/children")
NAMES = {"notion_search", "notion_read", "notion_append"}


def build(routes: dict[tuple[str, str], list[Any]]) -> tuple[dict[str, Any], FakeNotion]:
    n = FakeNotion(routes)
    group = NotionTool()
    group.bind(n.client())
    return {t.spec.name: t for t in group.tools()}, n


def search_routes() -> dict[tuple[str, str], list[Any]]:
    return {SEARCH: [ok(fx("notion_search.json"))]}


class _Requester:
    def __init__(self) -> None:
        self.asks: list[Any] = []

    async def request(self, ask: Any) -> AskOutcome:
        self.asks.append(ask)
        return AskOutcome.REQUESTED


class _Audit:
    def __init__(self) -> None:
        self.rows: list[AuditRecord] = []

    async def audit(self, rec: AuditRecord) -> None:
        self.rows.append(rec)


def dispatcher(
    tools: dict[str, Any],
    *,
    scopes: bool = True,
    req: _Requester | None = None,
    audit: _Audit | None = None,
) -> Dispatcher:
    return Dispatcher(
        tools,
        PolicyEngine(),
        scopes_enabled=LOCAL_SCOPES | ({"notion"} if scopes else set()),
        approvals=req,
        audit=audit,
    )


def test_three_tools_strict_schemas_and_declared_risk() -> None:
    tools = {t.spec.name: t for t in NotionTool().tools()}
    assert set(tools) == NAMES
    assert {n: t.risk for n, t in tools.items()} == {
        "notion_search": Risk.READ,
        "notion_read": Risk.READ,
        "notion_append": Risk.WRITE_EXTERNAL,
    }
    for t in tools.values():
        assert t.spec.parameters["additionalProperties"] is False
        assert len(t.spec.description) < 330
    assert set(tools["notion_append"].spec.parameters["properties"]) == {"page", "text"}


def test_no_update_delete_archive_share_tool_exists() -> None:
    bad = re.compile(r"update|delete|archive|share|permission|comment|move|create", re.IGNORECASE)
    assert not [n for n in NAMES if bad.search(n)]
    from src.integrations.notion.api import NotionClient

    methods = [m for m in dir(NotionClient) + dir(NotionTool) if not m.startswith("_")]
    assert not [m for m in methods if bad.search(m)]


async def test_unbound_fails_closed_and_is_not_offered() -> None:
    group = NotionTool()
    for t in group.tools():
        assert t.available() is False  # type: ignore[attr-defined]
        with pytest.raises(ToolError, match="unavailable"):
            await t.run({k: "x" for k in t.spec.parameters["properties"]})


async def test_search_text_has_only_count_and_handles_titles_go_external() -> None:
    tools, _ = build(search_routes())
    out = await tools["notion_search"].run({"query": "trip"})
    body = json.loads(out.text)
    assert body["count"] == 2 and all(HANDLE_RE.fullmatch(h) for h in body["pages"])
    for leak in ("Trip plan", "Ignore previous", "evil.example", P1, P2, "notion.so", "gmail_send"):
        assert leak not in out.text
    assert len(out.external) == 2 and all(isinstance(u, UntrustedText) for u in out.external)
    assert (
        "Trip plan" in out.external[0]._untrusted_value
        and "Ignore previous" in out.external[1]._untrusted_value
    )
    assert out.untrusted is True


async def test_search_never_exceeds_the_external_item_cap() -> None:
    page = fx("notion_search.json")["results"][0]
    many = {"results": [page | {"id": f"{i:08d}-1111-4111-8111-111111111111"} for i in range(30)]}
    tools, _ = build({SEARCH: [ok(many)]})
    out = await tools["notion_search"].run({"query": ""})
    assert len(out.external) <= MAX_EXTERNAL_ITEMS == 5
    assert json.loads(out.text)["count"] <= MAX_EXTERNAL_ITEMS


async def test_search_empty_has_no_external_item() -> None:
    tools, _ = build({SEARCH: [ok({"results": []})]})
    out = await tools["notion_search"].run({"query": ""})
    assert json.loads(out.text) == {
        "count": 0,
        "pages": [],
        "external_items_follow_page_order": True,
    }
    assert out.external == ()


async def test_read_content_only_in_external_and_text_has_no_id_or_title() -> None:
    tools, _ = build(
        {
            **search_routes(),
            PAGE1: [ok(fx("notion_page.json"))],
            CH1: [ok(fx("notion_children.json"))],
        }
    )
    d = dispatcher(tools)
    exposed = frozenset(tools)
    s = await d.dispatch("notion_search", {"query": "trip"}, exposed=exposed)
    handle = json.loads(s.content)["pages"][0]
    r = await d.dispatch("notion_read", {"page": handle}, exposed=exposed)
    assert r.status == "ok" and r.untrusted is True
    for leak in ("Flights", "Hotel", "Trip plan", P1):
        assert leak not in r.content
    (item,) = r.external
    assert "Flights booked" in item._untrusted_value and item._untrusted_value.startswith(
        "title: Trip plan"
    )


async def test_read_is_clipped_to_the_external_cap() -> None:
    block = fx("notion_children.json")["results"][1]
    block["paragraph"]["rich_text"][0]["plain_text"] = "y" * 1800
    tools, _ = build(
        {
            PAGE1: [ok(fx("notion_page.json"))],
            CH1: [ok({"results": [block] * 20, "has_more": False})],
        }
    )
    out = await tools["notion_read"].run({"page": P1})
    assert len(out.external[0]._untrusted_value) <= MAX_EXTERNAL_CHARS
    assert json.loads(out.text)["truncated"] is True


async def test_read_of_unshared_page_is_a_graceful_tool_error() -> None:
    tools, _ = build({PAGE1: [ok(fx("notion_error_404.json"), 404)]})
    with pytest.raises(ToolError, match="not shared"):
        await tools["notion_read"].run({"page": P1})


# ---------------------------------------------------------------- handles


def test_handle_book_is_opaque_bounded_strict_and_nonced() -> None:
    from src.tools.integrations import notion as mod

    book = HandleBook()
    hs = book.issue([P1, P2])
    assert all(HANDLE_RE.fullmatch(h) for h in hs) and book.resolve(hs[1]) == P2
    for bad in (P1, "n000000_9_9", "h1_1", hs[0] + " ", hs[0] + "\n", 5, None, ""):
        with pytest.raises(ToolError):
            book.resolve(bad)
    other = HandleBook()  # a "restarted" process: new nonce, old handles are stale
    other.issue([P1, P2])
    stale = hs[0].replace(book._nonce, "ffffff" if book._nonce != "ffffff" else "000000")
    with pytest.raises(ToolError):
        other.resolve(stale)
    for _ in range(mod.MAX_HANDLES):
        book.issue([P1])
    with pytest.raises(ToolError):
        book.resolve(hs[0])  # evicted


async def test_stale_handle_after_restart_fails_closed() -> None:
    tools, _ = build(search_routes())
    handle = json.loads((await tools["notion_search"].run({"query": ""})).text)["pages"][0]
    fresh_tools, n2 = build({})
    d = dispatcher(fresh_tools, req=_Requester())
    r = await d.dispatch("notion_read", {"page": handle}, exposed=frozenset(fresh_tools))
    assert r.status == "error" and n2.requests == []


async def test_raw_uuid_instead_of_handle_is_rejected_before_any_call() -> None:
    tools, n = build({PAGE1: [ok(fx("notion_page.json"))]})
    req = _Requester()
    d = dispatcher(tools, req=req)
    for name, args in (
        ("notion_read", {"page": P1}),
        ("notion_append", {"page": P1, "text": "x"}),
    ):
        r = await d.dispatch(name, args, exposed=frozenset(tools))
        assert r.status == "error" and r.rule_id == "tool.args"
    assert n.requests == [] and req.asks == []


async def test_resolve_rejection_still_writes_a_policy_audit_row() -> None:
    tools, _ = build({})
    audit = _Audit()
    d = dispatcher(tools, req=_Requester(), audit=audit)
    r = await d.dispatch("notion_read", {"page": "nope"}, exposed=frozenset(tools))
    assert r.status == "error"
    (row,) = audit.rows
    assert (row.kind, row.event, row.tool, row.outcome, row.executed) == (
        "decision",
        "args_rejected",
        "notion_read",
        "error",
        False,
    )


# ----------------------------------------------------------------- append


async def _search_then(
    tools: dict[str, Any], d: Dispatcher, name: str, args: dict[str, Any]
) -> Any:
    exposed = frozenset(tools)
    s = await d.dispatch("notion_search", {"query": ""}, exposed=exposed)
    handles = json.loads(s.content)["pages"]
    return await d.dispatch(name, {"page": handles[0], **args}, exposed=exposed)


async def test_append_goes_to_approval_with_the_true_payload_and_untrusted_target() -> None:
    tools, n = build({**search_routes(), PAGE1: [ok(fx("notion_page.json"))]})
    req = _Requester()
    d = dispatcher(tools, req=req)
    r = await _search_then(tools, d, "notion_append", {"text": "buy milk\r\nand eggs"})
    assert r.needs_approval and r.step_up and r.rule_id == "approval.write_external"
    (ask,) = req.asks
    assert ask.args["page"] == P1  # real id is what is stored and shown
    assert ask.args["text"] == "buy milk\nand eggs"  # exactly what will be written
    assert "UNTRUSTED" in ask.args["_target"] and "Trip plan" in ask.args["_target"]
    assert "PATCH" not in n.methods()  # nothing was written before approval


async def test_card_says_target_unverified_when_the_fetch_fails() -> None:
    tools, _ = build(search_routes())  # page route missing -> 404
    req = _Requester()
    d = dispatcher(tools, req=req)
    await _search_then(tools, d, "notion_append", {"text": "x"})
    assert req.asks[0].args["_target"].startswith("target unverified")


async def test_model_cannot_forge_the_target_line() -> None:
    tools, _ = build({**search_routes(), PAGE1: [ok(fx("notion_page.json"))]})
    req = _Requester()
    d = dispatcher(tools, req=req)
    await _search_then(tools, d, "notion_append", {"text": "x", "_target": "harmless: lunch"})
    assert "harmless" not in req.asks[0].args["_target"]


@pytest.mark.parametrize("text", ["a\u200bb", "a\u202eb", "a\x00b", "", "x" * 6001])
async def test_hidden_or_oversize_text_is_rejected_before_policy(text: str) -> None:
    tools, _ = build(search_routes())
    req = _Requester()
    d = dispatcher(tools, req=req)
    r = await _search_then(tools, d, "notion_append", {"text": text})
    assert r.status == "error" and r.rule_id == "tool.args" and req.asks == []


async def test_approved_append_runs_once_and_replay_does_not_duplicate() -> None:
    state: list[dict[str, Any]] = []

    def children(_r: Any) -> Any:
        return ok({"results": list(state), "has_more": False})

    def patch(r: Any) -> Any:
        for b in json.loads(r.content)["children"]:
            rich = b["paragraph"]["rich_text"]
            state.append(
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"plain_text": x["text"]["content"]} for x in rich]
                    },
                }
            )
        return ok({})

    tools, n = build({PAGE1: [ok(fx("notion_page.json"))], CH1: [children], PATCH1: [patch]})
    args = {"page": P1, "text": "buy milk"}
    with turn_event(uuid.UUID(int=42)):
        first = await tools["notion_append"].run(args)
        replay = await tools["notion_append"].run({**args, "_target": "x"})
    assert json.loads(first.text) == {"status": "appended"}
    assert json.loads(replay.text) == {"status": "already_appended"}
    assert n.methods().count("PATCH") == 1
    with turn_event(uuid.UUID(int=43)):  # a different turn is a different append
        again = await tools["notion_append"].run(args)
    assert json.loads(again.text) == {"status": "appended"}
    assert first.untrusted is False and first.external == ()


async def test_append_status_text_has_no_notion_strings() -> None:
    tools, _ = build(
        {
            PAGE1: [ok(fx("notion_page.json"))],
            CH1: [ok(fx("notion_children.json"))],
            PATCH1: [ok({})],
        }
    )
    with turn_event(uuid.UUID(int=1)):
        out = await tools["notion_append"].run({"page": P1, "text": "x"})
    assert set(json.loads(out.text)) == {"status"}


async def test_append_without_a_stored_turn_event_is_refused() -> None:
    tools, n = build({PAGE1: [ok(fx("notion_page.json"))], PATCH1: [ok({})]})
    with pytest.raises(ToolError, match="stored user turn"):
        await tools["notion_append"].run({"page": P1, "text": "x"})
    assert n.requests == []


async def test_failed_describe_hook_never_lets_a_model_target_reach_the_card() -> None:
    tools, _ = build({**search_routes(), PAGE1: [ok(fx("notion_page.json"))]})

    async def boom(_a: Any) -> Any:
        raise RuntimeError("fetch failed")

    tools["notion_append"].describe_args = boom
    req = _Requester()
    d = dispatcher(tools, req=req)
    await _search_then(tools, d, "notion_append", {"text": "x", "_target": "harmless: lunch"})
    (ask,) = req.asks
    assert "_target" not in ask.args and "harmless" not in json.dumps(dict(ask.args))


def test_notion_append_card_shows_true_payload_and_source_neutral_untrusted_target() -> None:
    from datetime import UTC, datetime, timedelta

    from src.approvals.card import render_card

    now = datetime(2026, 10, 29, 4, 0, tzinfo=UTC)
    payload = {
        "page": P1,
        "text": "buy milk",
        "_target": "UNTRUSTED page title from Notion, shown for identification only: title='T'",
    }
    card = render_card(
        tool="notion_append",
        action="call",
        payload=payload,
        expires_at=now + timedelta(minutes=10),
        now=now,
        offset_minutes=330,
        step_up=True,
        rule_id="approval.write_external",
    )
    assert '"text": "buy milk"' in card and P1 in card
    assert "Target (from the service, UNTRUSTED): UNTRUSTED page title from Notion" in card
    assert "Google" not in card


# ----------------------------------------------------------------- policy


@pytest.mark.parametrize("tool", sorted(NAMES))
async def test_scope_disabled_nothing_runs(tool: str) -> None:
    tools, n = build(search_routes())
    handle = json.loads((await tools["notion_search"].run({"query": ""})).text)["pages"][0]
    n.requests.clear()
    args = {"notion_search": {"query": ""}, "notion_read": {"page": handle}}.get(
        tool, {"page": handle, "text": "x"}
    )
    r = await dispatcher(tools, scopes=False).dispatch(tool, args, exposed=frozenset(tools))
    assert (r.status, r.rule_id) == ("denied", "deny.scope_disabled")
    assert n.requests == []


def _ctx(risk: Risk, mode: PolicyMode, tainted: bool) -> PolicyContext:
    return PolicyContext(
        exposed=True,
        tainted=tainted,
        mode=mode,
        scopes_enabled=LOCAL_SCOPES | {"notion"},
        calls_last_hour=0,
        external_recipient=risk is Risk.WRITE_EXTERNAL,
        destructive=risk is Risk.DESTRUCTIVE,
        quiet_hours=False,
    )


@pytest.mark.parametrize("tainted", [False, True])
@pytest.mark.parametrize("mode", list(PolicyMode))
@pytest.mark.parametrize("tool", sorted(NAMES))
def test_policy_matrix_modes_by_taint_by_tool(tool: str, mode: PolicyMode, tainted: bool) -> None:
    risk = {t.spec.name: t.risk for t in NotionTool().tools()}[tool]
    d = PolicyEngine().decide(ProposedCall(tool, {}, risk), _ctx(risk, mode, tainted))
    if mode is PolicyMode.KILLED:
        want = ("deny", "deny.killed", False)
    elif risk is Risk.READ:
        want = ("allow", "allow.read", False)
    elif mode is PolicyMode.READ_ONLY:
        want = ("deny", "deny.read_only", False)
    else:
        want = ("require_approval", "approval.write_external", True)
    assert (d.verdict, d.rule_id, d.step_up) == want


def test_policy_rows_exact_and_scope_is_not_local() -> None:
    rows = {n: r for n, r in TOOL_RULES.items() if n.startswith("notion_")}
    assert {n: (r.risk, r.scope, r.destructive, r.external_service) for n, r in rows.items()} == {
        "notion_search": (Risk.READ, "notion", False, True),
        "notion_read": (Risk.READ, "notion", False, True),
        "notion_append": (Risk.WRITE_EXTERNAL, "notion", False, True),
    }
    assert "notion" not in LOCAL_SCOPES


async def test_tools_offered_only_when_bound_and_scope_enabled() -> None:
    async def offered(group: NotionTool, **kw: object) -> set[str]:
        fake = FakeLLM(["ok"])
        o = Orchestrator(
            build_env(provider=fake).llm,
            group.tools(),
            PolicyEngine(),
            character_card="c",
            **kw,  # type: ignore[arg-type]
        )
        await o.run_turn("search my notion pages")
        return {t.name for t in fake.requests[0].tools}

    bound = NotionTool()
    bound.bind(FakeNotion().client())
    enabled = frozenset({"tasks", "reminders", "notion"})
    assert await offered(bound) == set()  # default wiring: local scopes only
    assert await offered(NotionTool(), scopes_enabled=enabled) == set()  # unbound
    assert await offered(bound, scopes_enabled=enabled) == NAMES


def test_group_is_discovered_and_bound_only_by_the_gated_integration_wiring() -> None:
    """Since the P3.0 follow-up the group is wired, but only through
    `src.app.integrations` (bound iff NOTION_TOKEN is set; see
    tests/unit/app/test_integrations_wiring.py). Nothing else in the app builds
    a NotionClient."""
    from pathlib import Path

    from src.tools.registry import ToolRegistry

    assert "notion" in ToolRegistry()
    app = Path(__file__).parents[3] / "src" / "app"
    builders = sorted(p.name for p in app.glob("*.py") if "NotionClient(" in p.read_text("utf-8"))
    assert builders == ["integrations.py"]
