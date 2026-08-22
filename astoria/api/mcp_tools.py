"""MCP tools (FastMCP, streamable HTTP at /mcp/) — the agent-facing surface of Astoria.

The docstrings below ARE the tool descriptions every agent sees, so they carry the usage
guidance. All tools route through `service.do_action`, exactly like REST, so the two
surfaces can never drift. Tools return dicts (FastMCP serialises them).
"""
from __future__ import annotations

import logging
from typing import Any

from astoria.api.auth import client_from_headers
from astoria.api.service import do_action

log = logging.getLogger("astoria.mcp")


def _client(default: str = "mcp") -> str:
    """Caller identity from the MCP HTTP request headers (Bearer token / X-Astoria-Client), else 'mcp'."""
    try:
        from fastmcp.server.dependencies import get_http_headers
        headers = get_http_headers() or {}
        if not headers:
            return default
        name = client_from_headers(headers)
        return default if name == "anonymous" else name
    except Exception:  # noqa: BLE001 — stdio transport / no request context
        return default


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in ("", None)}


def _res(r: Any) -> dict:
    if isinstance(r, dict):
        r.pop("status_code", None)
        return r
    return {"result": r}


def build_mcp():
    from fastmcp import FastMCP

    mcp = FastMCP("astoria")

    @mcp.tool
    def recall(query: str, user_id: str = "", layers: list[str] | None = None, limit: int = 12,
               max_tokens: int = 1000, as_of: str = "", include_profile: bool = False,
               session_id: str = "", facts_only: bool = False) -> dict:
        """Recall what Astoria remembers that is relevant to `query` — call this BEFORE answering anything
        that might depend on the user's preferences, setup, history, decisions or past conversations.

        Astoria is the shared long-term memory across ALL of the user's assistants (input, Claude Code,
        Nova, MegaPlan...): what one learns, all can recall. Layers searched (hybrid vector+BM25, ranked
        by relevance × recency × importance × trust):
          profile     — durable facts about the user themself (name, location, favorites, preferences)
          semantic    — facts about the world the user cares about (tools, hardware, projects, decisions)
          procedural  — how-tos / learned procedures (may carry a `ref` to a skill, plan or doc)
          episodic    — summaries and raw turns of past conversations (may be outdated)
        Pass `session_id` to also get the last turns of THIS session as `working` memory.

        Returns {items:[{id, layer, kind, text, subject, predicate, value, score, confidence, source,
        asserted_at, valid_from, valid_to, occurred_at, is_belief, stale_hint}], working, profile,
        context, health, snapshot_id}. **`context` is a pre-rendered block — inject it VERBATIM into your
        prompt/system context**; current facts in it are authoritative, past-conversation lines may be
        stale (`stale_hint`). Empty memory → context "". `as_of` (ISO date) answers "what was true then".
        Set include_profile=true to get the user's profile narrative+facts as a stable prefix.
        """
        p = _clean(dict(query=query, user_id=user_id, layers=layers, limit=limit, max_tokens=max_tokens,
                        as_of=as_of, include_profile=include_profile, session_id=session_id, facts_only=facts_only))
        return _res(do_action("recall", p, _client()))

    @mcp.tool
    def capture(text: str = "", user_input: str = "", agent_response: str = "", kind: str = "note",
                user_id: str = "", session_id: str = "", source: str = "", importance: float = 0.5,
                tags: list[str] | None = None, cognify: bool = True, priority: str = "normal") -> dict:
        """Capture raw experience into memory (non-lossy, durable, cheap): a conversation turn
        (`kind="turn"`, pass user_input + agent_response and a stable `session_id` per conversation), a
        `summary` you wrote of a session, or a free-form `note`. The episode is stored FIRST; a background
        worker later extracts durable (subject, predicate, value) facts from it with an LLM — so you
        don't need to decide what's durable: just capture. Obvious corrections ("actually my favorite beer
        is IPA", "forget that I ...") are detected immediately and applied as explicit facts.
        Re-sending the same content is deduped (idempotent). Trivial/command text ("ok", "/help") is
        dropped. Returns {episode_id, deduped, dropped?, detector?, queued}. Use `remember` instead
        when you already have an explicit fact to store.
        """
        p = _clean(dict(text=text, user_input=user_input, agent_response=agent_response, kind=kind,
                        user_id=user_id, session_id=session_id, source=source, importance=importance,
                        tags=tags, cognify=cognify, priority=priority))
        return _res(do_action("capture", p, _client()))

    @mcp.tool
    def remember(subject: str, predicate: str, value: str, user_id: str = "", valid_from: str = "",
                 valid_to: str = "", retract: bool = False, layer: str = "", confidence: float | None = None,
                 tags: list[str] | None = None) -> dict:
        """Store (or, with retract=true, withdraw) one EXPLICIT durable fact as a (subject, predicate, value)
        triple — the high-trust path for things the user states or you are certain of.

          subject   — who/what it is about. First person ("I", "me", "my", "user") → the user; write the
                      user_id (the configured default user) for facts about the user; a topic/tool/project name otherwise.
          predicate — snake_case relation: favorite_beer, location, uses_tool, owns_hardware, decided,
                      works_on_project, likes, learned_howto... Unknown predicates are auto-registered.
                      favorite_/default_/primary_/preferred_/current_ predicates hold ONE current value
                      (a new value supersedes the old, history kept); others accumulate members.
          value     — the value text.  valid_from/valid_to — optional ISO dates for when it was/is true
                      (a past valid_to records history without changing the present).
        Returns {fact, action: inserted|superseded|noop|historical, superseded:[ids]}.
        retract=true closes the belief ("we no longer hold this") — the same as `forget(subject,
        predicate, value)` but keeps the row visible in history. Memory is shared across input /
        Claude Code / Nova: remembering here makes it available everywhere.
        """
        p = _clean(dict(subject=subject, predicate=predicate, value=value, user_id=user_id,
                        valid_from=valid_from, valid_to=valid_to, layer=layer, confidence=confidence, tags=tags))
        if retract:
            p.pop("valid_from", None); p.pop("valid_to", None); p.pop("layer", None); p.pop("confidence", None)
            return _res(do_action("retract", p, _client()))
        return _res(do_action("fact_add", p, _client()))

    @mcp.tool
    def forget(fact_id: str = "", subject: str = "", predicate: str = "", value: str = "", query: str = "",
               mode: str = "soft", user_id: str = "") -> dict:
        """Forget facts on the user's request ("forget that...", "delete what you know about X").
        Target by `fact_id` (from recall items), by triple (subject[, predicate[, value]]), or by a
        free-text `query` (top matches). mode="soft" archives (hidden from recall, still auditable);
        mode="hard" deletes the row for good — use only when the user explicitly wants it gone.
        Either way a tombstone stops the fact being re-learned from old conversations; only an explicit
        `remember` re-asserts it. Prefer `remember(..., retract=true)` when the fact WAS true and just
        stopped being true (keeps history); use forget when it should never have been stored.
        Returns {forgotten:[{id, subject, predicate, value}]}. For a vague natural-language request
        ("forget the beer stuff") prefer memory(action="resolve", text=...) → show targets → resolve_apply.
        """
        p = _clean(dict(fact_id=fact_id, subject=subject, predicate=predicate, value=value, query=query,
                        mode=mode, user_id=user_id))
        return _res(do_action("forget", p, _client()))

    @mcp.tool
    def memory(action: str, user_id: str = "", fact_id: str = "", subject: str = "", predicate: str = "",
               value: str = "", status: str = "", layer: str = "", q: str = "", at: str = "",
               as_believed_at: str = "", limit: int = 50, max_tokens: int = 1200, name: str = "",
               cardinality: str = "", layer_hint: str = "", confidence: float | None = None,
               importance: float | None = None, valid_from: str = "", valid_to: str = "",
               tags: list[str] | None = None, session_id: str = "", kind: str = "",
               text: str = "", confirm: bool = False, plan: dict | None = None, node: str = "",
               depth: int | None = None, src: str = "", relation: str = "", dst: str = "", edge_id: str = "",
               alias: str = "", canonical: str = "", evidence: str = "") -> dict:
        """Admin / inspection dispatcher over the memory store. Set `action` and pass only what it needs:
          resolve     text — NATURAL-LANGUAGE memory instruction ("forget the beer stuff", "actually I
                      moved to Oakland", "I don't use Emacs anymore") → an LLM resolves WHICH facts are
                      meant and returns a plan {intent: forget|retract|correct|remember|none, targets:[facts],
                      new_fact, confidence, explanation, requires_confirmation}. NOTHING is applied — show
                      the user the targets when requires_confirmation is true, then call resolve_apply.
          resolve_apply  plan (from resolve) — apply it; or text [+ confirm=true] — resolve and apply in
                      one step (applies without confirm only when the plan is unambiguous). Use these
                      when `forget`/`remember` would need a subject/predicate you don't have.
          list        [subject, predicate, status(active|staging|superseded|retracted|archived|any), layer, q, limit]
                      — browse facts.               get  fact_id — one fact.
          update      fact_id[, value, status, confidence, importance, layer, valid_from, valid_to, tags]
          history     subject, predicate — full supersession chain (newest first).
          as_of       at[, as_believed_at, subject, predicate] — what was true at a date (bitemporal).
          profile     — the user's profile narrative + profile-layer facts.
          briefing    [max_tokens] — stable session-start context block (narrative + top facts).
          predicates  [name + cardinality/layer_hint to update] — the predicate registry.
          approve     fact_id — promote a low-confidence `staging` fact to active.
          episodes    [session_id, kind, limit] — list raw episodes.
          audit       [limit] — the mutation log.            health — service/DB/TEI/LLM status.
        Graph layer (typed links between subjects/entities and facts; nodes are an entity name,
        "entity:<name>" or "fact:<uuid>"):
          graph       node[, depth] — the neighbourhood of a node: {root, nodes[{id,kind,hops,via,label}], edges}.
          edges       [node, relation, depth] — list edges (touching node / within depth hops).
          edge_add    src, relation, dst[, confidence, evidence] — assert a link ("buildbot" runs_on "workstation-1";
                      relations snake_case: part_of, located_in, works_at, owns, runs_on, depends_on, related_to).
          edge_delete edge_id — retract an edge.
          aliases     [canonical] — subject aliases (alias → canonical name).
          alias_add   alias, canonical — declare two names the same subject (rename / aka); every later write
                      or read on `alias` lands on `canonical`.          alias_delete alias.
        """
        a = (action or "").strip().lower()
        p = _clean(dict(user_id=user_id, fact_id=fact_id, subject=subject, predicate=predicate, value=value,
                        status=status, layer=layer, q=q, at=at, as_believed_at=as_believed_at, limit=limit,
                        max_tokens=max_tokens, name=name, cardinality=cardinality, layer_hint=layer_hint,
                        confidence=confidence, importance=importance, valid_from=valid_from, valid_to=valid_to,
                        tags=tags, session_id=session_id, kind=kind, text=text, confirm=confirm, plan=plan,
                        node=node, depth=depth, src=src, relation=relation, dst=dst, edge_id=edge_id, alias=alias,
                        canonical=canonical, evidence=evidence))
        table = {"list": "facts_list", "get": "fact_get", "update": "fact_update", "history": "history",
                 "as_of": "as_of", "profile": "profile", "briefing": "briefing", "approve": "approve",
                 "audit": "audit", "health": "health", "episodes": "episodes_list", "facts": "facts_list",
                 "delete": "fact_delete", "resolve": "resolve", "resolve_apply": "resolve_apply",
                 "graph": "graph", "edges": "edges_list", "edge_add": "edge_add", "edge_delete": "edge_delete",
                 "aliases": "aliases_list", "alias_add": "alias_add", "alias_delete": "alias_delete"}
        if a == "predicates":
            svc = "predicate_update" if (name and (cardinality or layer_hint)) else "predicates_list"
        elif a in table:
            svc = table[a]
        else:
            return {"error": f"unknown action {action!r}; valid: resolve|resolve_apply|list|get|update|history|"
                             f"as_of|profile|briefing|predicates|approve|episodes|audit|health|graph|edges|"
                             f"edge_add|edge_delete|aliases|alias_add|alias_delete"}
        r = do_action(svc, p, _client())
        if isinstance(r, list):
            return {"action": a, "count": len(r), "items": r}
        return _res(r)

    # ---- MemoryOS compat (drop-in for the old memoryos MCP server) ------------

    @mcp.tool
    def retrieve_memory(user_id: str, query: str) -> dict:
        """(MemoryOS-compatible) Retrieve memory relevant to `query`: short_term_history (recent turns),
        retrieved_pages (past conversation), retrieved_user_knowledge (facts) and user_profile.
        New integrations should prefer `recall` (returns a ready-to-inject `context` block)."""
        return _res(do_action("retrieve", {"user_id": user_id, "query": query}, _client()))

    @mcp.tool
    def add_memory(user_id: str, user_input: str, agent_response: str, timestamp: str = "") -> dict:
        """(MemoryOS-compatible) Store one conversation turn (user_input + agent_response). Equivalent to
        `capture(kind="turn", ...)`; durable facts are extracted in the background."""
        p = _clean(dict(user_id=user_id, user_input=user_input, agent_response=agent_response, timestamp=timestamp))
        return _res(do_action("memories_add", p, _client()))

    @mcp.tool
    def get_user_profile(user_id: str) -> dict:
        """(MemoryOS-compatible) The user's profile narrative ("None" when nothing is known yet).
        Prefer `memory(action="profile")` for the structured profile facts."""
        return _res(do_action("user_profile", {"user_id": user_id}, _client()))

    return mcp
