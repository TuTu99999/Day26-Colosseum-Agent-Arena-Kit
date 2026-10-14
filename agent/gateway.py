"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

THE STARTER'S SHAPE (read this before you start editing `decide()`)
----------------------------------------------------------------------------
This starter FORWARDS ALMOST EVERYTHING AND DENIES NOTHING. That is not a
placeholder oversight — it is the honest zero-defence baseline you are
meant to beat: `bots/rookie` in the kit's own ladder does exactly the same
thing, and RULES.md's own words are "if you cannot beat Rookie you have a
bug, not a strategy." `decide()` below is structured as four named jobs —
ROUTE, ADMIT, AUTHORIZE, BUDGET — each with a one-line TODO naming what a
real implementation checks and why. None of the four currently rejects,
rewrites, or reroutes anything; they are seams, not solutions. Fill them in
using `agent/strategy.py` (routing/budget policy) and `agent/guardrails.py`
(the safety checks) — both already import cleanly from here.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry
from agent.strategy import cheap_mask, is_catalog_trap, pick_replica, successor_of

try:
    from kit.mcp.specs import TOOL_SPECS, cost as _tool_cost
    _SPECS_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    TOOL_SPECS = {}
    _SPECS_AVAILABLE = False

    def _tool_cost(
        server: str,
        tool: str,
        fields: tuple[str, ...] = (),
        n_rows: int = 1,
    ) -> int:
        return 5


A2A_SERVERS: frozenset[str] = frozenset(
    {"curriculum-analyst", "citation-checker", "roster"}
)

# These are the state-changing calls exposed by the kit.  Two are present in
# kit.mcp.servers but intentionally absent from the smaller public price table,
# so the gateway keeps the complete enforcement set locally.
WRITE_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("content", "file_content_bug"),
        ("content", "flag_stale_slide"),
        ("progress", "record_mastery"),
        ("registry", "pin"),
    }
)

# A catalog's default mask is deliberately its full dump.  These are the
# smallest useful discovery masks and avoid turning a harmless DISCOVER into
# a whole-round bill.
CATALOG_MASKS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("registry", "list_servers"): ("name",),
    ("glossary", "list_terms"): ("term",),
}

# Credits are more valuable late in a duel.  This is a per-call ceiling, not a
# per-round call quota: a normal locate/read/corroborate plan must still fit.
ROUND_CALL_ALLOWANCE: Mapping[int, int] = {
    1: 8, 2: 8, 3: 8, 4: 9, 5: 9, 6: 9, 7: 10, 8: 11, 9: 11, 10: 12,
}

_INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous instruction",
    "ignore all previous",
    "disregard the above",
    "system override",
    "you must now",
    "also record this for",
    "reveal the",
    "bỏ qua mọi hướng dẫn",
)

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance attributes below are this starter's ENTIRE per-duel memory —
    all currently unused by `decide()`'s naive body, but declared here
    (rather than invented ad hoc later) so the four TODO jobs below have
    somewhere obvious to keep state once you implement them. `agent/
    strategy.py` has working building blocks for exactly this (a budget
    pacer, a result cache, a replica-choice heuristic) — this starter does
    not wire them in for you; that wiring is the assignment.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)
        self._seen_anchors: dict[str, Any] = {}
        self._credits_authorised: int = 0
        self._denied_cmd_ids: set[str] = set()
        self._admitted_cards: dict[str, dict[str, Any]] = {}
        self._etags: dict[str, str] = {}
        self._used_idempotency_keys: set[str] = set()
        self._rate_calls: dict[tuple[str, str], list[int]] = {}

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything you need to
        decide is already sitting in `cmd` and `self.ctx`.

        The implementation is deliberately fail-closed for commands that are
        provably unsafe or doomed, and fail-open for unfamiliar read-only tools.
        That balance matters: blanket denial loses on clean blank cards."""
        try:
            self._telemetry.decision_seen(cmd)

            # JOB 1 — ROUTE.  A body-selected route is precisely the
            # header_spoof/replica_flip shape: do not legitimise it by copying
            # it into a trusted header.  Ordinary MCP calls are pinned through
            # the header using the strategy helper.
            if any(cmd.args.get(k) not in (None, "") for k in ("route", "_route", "replica")):
                return self.deny(cmd, "route declared in the body, not the trusted header")

            routed = self._route(cmd)

            # JOB 2 — ADMIT.  Validate the A2A identity envelope, reject
            # mutation markers visible in the command, and avoid charged calls
            # that cannot possibly succeed.
            admission_error, quarantine = self._admission_error(routed)
            if admission_error:
                return self.deny(cmd, admission_error, quarantine=quarantine)

            # JOB 3 — AUTHORIZE.  Authority is derived from ctx.act and
            # ctx.scopes, never from the tutor's powerful ctx.sub identity.
            authorization_error = self._authorization_error(routed)
            if authorization_error:
                return self.deny(cmd, authorization_error)

            # JOB 4 — BUDGET.  Make masks explicit, replace punishment-button
            # masks with narrow useful ones, and refuse only when even the
            # narrow form cannot fit the live credit/round allowance.
            budgeted, budget_error = self._budget(routed)
            if budget_error:
                return self.deny(cmd, budget_error)

            call = self._to_tool_call(budgeted)
            changed = budgeted != cmd
            decision = Decision(
                verdict="rewrite" if changed else "forward",
                call=call,
                note="gateway policy rewrite" if changed else None,
            )
            self._record_authorised(budgeted)
            self._telemetry.decision_made(cmd, decision)
            return decision
        except Exception as exc:
            # decide() raising is itself a scored integrity failure.  A malformed
            # or future command must therefore degrade to a valid, free denial.
            return self.deny(cmd, f"gateway policy could not safely classify command: {type(exc).__name__}")

    def _route(self, cmd: Command) -> Command:
        server, tool = cmd.server, cmd.tool
        successor = successor_of(server, tool)
        if successor is not None:
            server, tool = successor

        headers = {str(k).lower(): v for k, v in cmd.headers.items()}
        headers.pop("x-mcp-body-route", None)
        if server not in A2A_SERVERS:
            path_id = self._path_id(cmd.args)
            known_drifting = bool(
                cmd.args.get("known_drifting") or cmd.args.get("drifts")
            )
            fresher = cmd.args.get("fresher")
            choice = pick_replica(
                path_id=path_id,
                known_drifting=known_drifting,
                # A drift flag proves disagreement, not that canonical is
                # newer (day18 is the counterexample).  Stay on working
                # unless provenance/current_version_of supplied `fresher`.
                prefers_fresh=False,
            )
            replica = fresher if fresher in ("w", "c") else choice.replica
            headers["mcp-replica"] = replica

        key = (server, tool)
        spec = TOOL_SPECS.get(key) if _SPECS_AVAILABLE else None
        if key in WRITE_TOOLS or bool(getattr(spec, "is_write", False)):
            anchor = str(cmd.args.get("anchor") or cmd.args.get("learner") or "")
            if not headers.get("if-match") and anchor in self._etags:
                headers["if-match"] = self._etags[anchor]
            if not headers.get("idempotency-key") and headers.get("if-match"):
                headers["idempotency-key"] = cmd.cmd_id

        return replace(cmd, server=server, tool=tool, headers=headers)

    @staticmethod
    def _path_id(args: Mapping[str, Any]) -> str | None:
        direct = args.get("path_id")
        if isinstance(direct, str) and direct:
            return direct
        for key in ("anchor", "concept"):
            raw = args.get(key)
            if not isinstance(raw, str) or ":" not in raw:
                continue
            tail = raw.split(":", 1)[1]
            slug = tail.split("/", 1)[0]
            if slug:
                return slug
        return None

    def _admission_error(self, cmd: Command) -> tuple[str | None, bool]:
        headers = {str(k).lower(): v for k, v in cmd.headers.items()}

        if str(headers.get("x-server-fingerprint", "")).lower() in {
            "invalid", "untrusted", "unvouched", "forged",
        }:
            return "server identity is not vouched for", False
        if str(headers.get("x-card-signature", "")).lower() in {
            "invalid", "untrusted", "unvouched", "forged",
        }:
            return "peer Agent Card signature is invalid", False
        if cmd.args.get("peer_unverified") is True:
            return "peer result is explicitly unverified", False

        blob = " ".join(str(v) for v in cmd.args.values()).lower()
        if any(marker in blob for marker in _INJECTION_MARKERS):
            return "instruction-shaped content was quarantined", True
        catalog = cmd.args.get("catalog")
        if isinstance(catalog, (str, bytes, list, tuple, dict)) and len(catalog) > 512:
            return "inflated catalog rejected before it consumed the context budget", False

        if cmd.server in A2A_SERVERS:
            card = self._admitted_cards.get(cmd.server)
            if not card or not card.get("verified"):
                return "peer Agent Card was not admitted by the registry", False
            if cmd.tool not in set(card.get("skills") or ()):
                return "requested skill is not declared on the admitted Agent Card", False
            aud = headers.get("aud")
            if aud not in (cmd.server, f"a2a:{cmd.server}", f"mcp:{cmd.server}"):
                return "delegation audience does not match the peer being called", False

        spec = TOOL_SPECS.get((cmd.server, cmd.tool)) if _SPECS_AVAILABLE else None
        needs_lease = bool(getattr(spec, "needs_lease", False)) or (
            cmd.server, cmd.tool
        ) == ("slides", "get_frame")
        if needs_lease:
            live = set(getattr(self.ctx, "leases", ()) or ())
            if not cmd.lease_id or cmd.lease_id not in live:
                return "slides.get_frame requires a live lease from this exchange", False

        if self._unchanged_conflict_in_history(cmd):
            return "unchanged retry after a write conflict requires fresh provenance", False
        if self._rate_limited(cmd):
            return "tool rate window is exhausted; a charged failure was avoided", False
        return None, False

    def _authorization_error(self, cmd: Command) -> str | None:
        act = str(getattr(self.ctx, "act", "") or "")
        for key in ("learner", "learner_id", "act", "target", "subject"):
            target = cmd.args.get(key)
            if not isinstance(target, str) or not target:
                continue
            if key in {"target", "subject"} and "learner:" not in target.lower():
                continue
            if target.lower() != act.lower():
                return f"target {target!r} is not owned by ctx.act={act!r}"

        key = (cmd.server, cmd.tool)
        spec = TOOL_SPECS.get(key) if _SPECS_AVAILABLE else None
        is_write = key in WRITE_TOOLS or bool(getattr(spec, "is_write", False))
        if not is_write:
            return None

        required_scope = f"wiki.write:{cmd.server}"
        scopes = set(getattr(self.ctx, "scopes", ()) or ())
        if required_scope not in scopes:
            return f"write requires missing scope {required_scope!r}"

        headers = {str(k).lower(): v for k, v in cmd.headers.items()}
        if not headers.get("if-match") or not headers.get("idempotency-key"):
            return "write requires a fresh If-Match etag and Idempotency-Key"
        if str(headers["idempotency-key"]) in self._used_idempotency_keys:
            return "Idempotency-Key was already used this duel"
        return None

    def _budget(self, cmd: Command) -> tuple[Command, str | None]:
        key = (cmd.server, cmd.tool)
        spec = TOOL_SPECS.get(key) if _SPECS_AVAILABLE else None
        requested = tuple(cmd.fields)

        if key in CATALOG_MASKS and is_catalog_trap(cmd.server, cmd.tool, requested):
            fields = cheap_mask(cmd.server, cmd.tool, CATALOG_MASKS[key])
        elif spec is not None:
            if requested == ("*",):
                fields = tuple(spec.default_fields)
            elif not requested:
                fields = tuple(spec.default_fields)
            else:
                valid = tuple(f for f in requested if f in spec.all_fields)
                fields = cheap_mask(cmd.server, cmd.tool, valid or tuple(spec.default_fields))
        else:
            fields = requested

        candidate = replace(cmd, fields=fields)
        cost = self._estimate_cost(candidate)
        allowance = ROUND_CALL_ALLOWANCE.get(int(getattr(self.ctx, "round", 0) or 0), 9)

        if cost > allowance and spec is not None:
            narrower = CATALOG_MASKS.get(key, tuple(spec.default_fields))
            candidate = replace(candidate, fields=tuple(narrower))
            cost = self._estimate_cost(candidate)

        credits = int(getattr(self.ctx, "credits", 0) or 0)
        if cost > credits:
            return candidate, f"call costs {cost} credits but only {credits} remain"
        if cost > allowance:
            return candidate, f"call costs {cost} credits, above round-{getattr(self.ctx, 'round', 0)} allowance {allowance}"
        return candidate, None

    @staticmethod
    def _estimate_cost(cmd: Command) -> int:
        if not _SPECS_AVAILABLE or (cmd.server, cmd.tool) not in TOOL_SPECS:
            return 5
        return _tool_cost(cmd.server, cmd.tool, fields=tuple(cmd.fields), n_rows=1)

    def _rate_limited(self, cmd: Command) -> bool:
        spec = TOOL_SPECS.get((cmd.server, cmd.tool)) if _SPECS_AVAILABLE else None
        window = getattr(spec, "rate_limit", None)
        if window is None:
            return False
        allowed, per_rounds = window
        current = int(getattr(self.ctx, "round", 0) or 0)
        recent = [r for r in self._rate_calls.get((cmd.server, cmd.tool), []) if current - r < per_rounds]
        self._rate_calls[(cmd.server, cmd.tool)] = recent
        return len(recent) >= allowed

    def _record_authorised(self, cmd: Command) -> None:
        cost = self._estimate_cost(cmd)
        self._credits_authorised += cost
        spec = TOOL_SPECS.get((cmd.server, cmd.tool)) if _SPECS_AVAILABLE else None
        if getattr(spec, "rate_limit", None) is not None:
            self._rate_calls.setdefault((cmd.server, cmd.tool), []).append(
                int(getattr(self.ctx, "round", 0) or 0)
            )
        key = (cmd.server, cmd.tool)
        if key in WRITE_TOOLS or bool(getattr(spec, "is_write", False)):
            idem = {str(k).lower(): v for k, v in cmd.headers.items()}.get("idempotency-key")
            if idem:
                self._used_idempotency_keys.add(str(idem))

    @staticmethod
    def _command_signature(value: Any) -> tuple[Any, ...] | None:
        if isinstance(value, Command):
            return value.server, value.tool, repr(sorted(value.args.items())), tuple(value.fields)
        if isinstance(value, Mapping):
            server, tool = value.get("server"), value.get("tool")
            if not isinstance(server, str) or not isinstance(tool, str):
                return None
            args = value.get("args") if isinstance(value.get("args"), Mapping) else {}
            fields = value.get("fields") or ()
            return server, tool, repr(sorted(args.items())), tuple(fields)
        return None

    def _unchanged_conflict_in_history(self, cmd: Command) -> bool:
        wanted = self._command_signature(cmd)
        for item in getattr(self.ctx, "history", ()) or ():
            if not isinstance(item, Mapping):
                continue
            prior = item.get("command") or item.get("cmd")
            result = item.get("result") or item.get("outcome") or {}
            error = result.get("error") if isinstance(result, Mapping) else None
            code = error.get("code") if isinstance(error, Mapping) else None
            if code in {"conflict", "precondition_missing"} and self._command_signature(prior) == wanted:
                return True
        return False

    def note_card(self, server: str, card: Mapping[str, Any]) -> None:
        """Record an Agent Card already verified by the trusted registry path."""
        self._admitted_cards[str(server)] = dict(card)

    def note_provenance(self, anchor: str, etag: str) -> None:
        """Record the latest etag returned after a successful provenance read."""
        if anchor and etag:
            self._etags[str(anchor)] = str(etag)

    # Operator and existing harnesses use this older callback spelling.
    note_result = note_provenance

    def deny(self, cmd: Command, reason: str, *, quarantine: bool = False) -> Decision:
        """Not called anywhere in this starter's `decide()` — a ready-made
        helper for when you fill in JOB 2 / JOB 3 above, so denying doesn't
        mean hand-building a `Decision` inline at every call site. Kept as
        a real method (not a stub) because the shape of a correct denial —
        no `call`, a non-empty `reason` — is exactly the thing worth
        getting right by construction rather than by convention."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason, quarantine=quarantine)
        try:
            self._telemetry.decision_made(cmd, decision)
        except Exception:
            pass
        return decision

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http "
            "fields=anchor,course_day,track header.aud=curriculum-analyst",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — route/admit/authorize/budget policy ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    gw.note_card(
        "curriculum-analyst",
        {"verified": True, "skills": ["which_days_cover"]},
    )
    for cmd in demo_commands:
        decision = gw.decide(cmd)
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} quarantine={decision.quarantine}")
        assert decision.verdict in {"forward", "rewrite"}
        assert decision.call is not None
        call_dict = decision.call.to_dict() if hasattr(decision.call, "to_dict") else decision.call
        expected = successor_of(cmd.server, cmd.tool) or (cmd.server, cmd.tool)
        assert (call_dict["server"], call_dict["tool"]) == expected

    print(f"\n=== Gateway.deny — the free fail-closed path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
