"""REST surface (docs/CONTRACT.md table) — thin typed routes over `service.do_action`.

Every route resolves the caller (`auth.client_from_request`), forwards to the one dispatcher
and maps `{"error": ...}` results to 4xx/503 JSON. `/op` is the raw mirror for scripts.
TEI/LLM outages never 500: the store degrades (no vector) and health reports it.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from astoria.api.auth import client_from_request, require_write_token, WriteNotAuthorized
from astoria.api.service import do_action

router = APIRouter()


def _respond(r: Any, ok_status: int = 200) -> Any:
    """Service result → response. Error dicts carry an advisory status_code (default 400)."""
    if isinstance(r, dict) and r.get("error"):
        code = int(r.pop("status_code", 400) or 400)
        return JSONResponse(status_code=code, content=r)
    if isinstance(r, dict):
        r.pop("status_code", None)
    return JSONResponse(status_code=ok_status, content=r)


def _run(request: Request, action: str, params: dict | None = None, **extra) -> Any:
    p = dict(params or {})
    p.update({k: v for k, v in extra.items() if v is not None})
    try:
        require_write_token(request.headers, action)
    except WriteNotAuthorized as e:
        return JSONResponse({"error": str(e)}, status_code=401)
    return _respond(do_action(action, p, client_from_request(request)))


class _Body(BaseModel):
    model_config = ConfigDict(extra="allow")

    def params(self) -> dict:
        return self.model_dump(exclude_none=True)


class RecallReq(_Body):
    user_id: str | None = None
    query: str = ""
    session_id: str | None = None
    layers: list[str] | str | None = None
    max_tokens: int = 1000
    limit: int = 12
    facts_only: bool = False
    include_profile: bool = False
    as_of: str | None = None
    as_believed_at: str | None = None


class CaptureReq(_Body):
    user_id: str | None = None
    kind: str = "turn"
    text: str | None = None
    user_input: str | None = None
    agent_response: str | None = None
    source: str | None = None
    session_id: str | None = None
    occurred_at: str | None = None
    importance: float | None = None
    tags: list[str] | None = None
    meta: dict | None = None
    cognify: bool = True
    priority: str = "normal"


class FactReq(_Body):
    user_id: str | None = None
    subject: str
    predicate: str
    value: str
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float | None = None
    layer: str | None = None
    tags: list[str] | None = None
    cardinality: str | None = None
    historical: bool = False


class RetractReq(_Body):
    user_id: str | None = None
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None
    fact_id: str | None = None


class ForgetReq(_Body):
    user_id: str | None = None
    fact_id: str | None = None
    query: str | None = None
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None
    mode: str = "soft"


class ApproveReq(_Body):
    user_id: str | None = None
    fact_id: str


class AsOfReq(_Body):
    user_id: str | None = None
    at: str
    as_believed_at: str | None = None
    subject: str | None = None
    predicate: str | None = None
    query: str | None = None
    limit: int = 50


class PredicatePatch(_Body):
    cardinality: str | None = None
    layer_hint: str | None = None
    description: str | None = None


class ResolveReq(_Body):
    user_id: str | None = None
    text: str = ""
    limit: int = 8


class ResolveApplyReq(_Body):
    user_id: str | None = None
    text: str | None = None
    plan: dict | None = None
    confirm: bool = False
    limit: int = 8


class EdgeReq(_Body):
    user_id: str | None = None
    src: str
    relation: str
    dst: str
    src_kind: str | None = None
    dst_kind: str | None = None
    weight: float | None = None
    confidence: float | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    evidence: str | None = None
    meta: dict | None = None


class AliasReq(_Body):
    user_id: str | None = None
    alias: str
    canonical: str


class RetrieveReq(_Body):
    user_id: str | None = None
    query: str = ""


class MemoriesReq(_Body):
    user_id: str | None = None
    user_input: str = ""
    agent_response: str = ""
    timestamp: str | None = None
    session_id: str | None = None


# ---- health -----------------------------------------------------------------

@router.get("/health")
def health(request: Request):
    r = do_action("health", {}, client_from_request(request))
    code = 503 if r.get("status") != "ok" else 200
    r.pop("status_code", None)
    return JSONResponse(status_code=code, content=r)


# ---- memory read/write ------------------------------------------------------

@router.post("/recall")
def recall(req: RecallReq, request: Request):
    return _run(request, "recall", req.params())


@router.post("/capture")
def capture(req: CaptureReq, request: Request):
    return _run(request, "capture", req.params())


@router.get("/briefing")
def briefing(request: Request, user_id: str | None = None, max_tokens: int = 1200):
    return _run(request, "briefing", user_id=user_id, max_tokens=max_tokens)


@router.get("/profile")
def profile(request: Request, user_id: str | None = None):
    return _run(request, "profile", user_id=user_id)


# ---- facts ------------------------------------------------------------------

@router.get("/facts")
def facts_list(request: Request, user_id: str | None = None, subject: str | None = None,
               predicate: str | None = None, status: str | None = "active", layer: str | None = None,
               q: str | None = None, limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0)):
    return _run(request, "facts_list", user_id=user_id, subject=subject, predicate=predicate, status=status,
                layer=layer, q=q, limit=limit, offset=offset)


@router.post("/facts")
def fact_add(req: FactReq, request: Request):
    return _run(request, "fact_add", req.params())


@router.get("/facts/{fact_id}")
def fact_get(fact_id: str, request: Request, user_id: str | None = None):
    return _run(request, "fact_get", fact_id=fact_id, user_id=user_id)


@router.patch("/facts/{fact_id}")
def fact_update(fact_id: str, body: dict, request: Request):
    p = dict(body or {})
    p["fact_id"] = fact_id
    return _run(request, "fact_update", p)


@router.delete("/facts/{fact_id}")
def fact_delete(fact_id: str, request: Request, user_id: str | None = None, mode: str = "soft"):
    return _run(request, "fact_delete", fact_id=fact_id, user_id=user_id, mode=mode)


@router.post("/correct")
def correct(req: FactReq, request: Request):
    return _run(request, "correct", req.params())


@router.post("/retract")
def retract(req: RetractReq, request: Request):
    return _run(request, "retract", req.params())


@router.post("/forget")
def forget(req: ForgetReq, request: Request):
    return _run(request, "forget", req.params())


@router.post("/approve")
def approve(req: ApproveReq, request: Request):
    return _run(request, "approve", req.params())


@router.get("/history")
def history(request: Request, user_id: str | None = None, subject: str | None = None, predicate: str = ""):
    return _run(request, "history", user_id=user_id, subject=subject, predicate=predicate)


@router.post("/as_of")
def as_of(req: AsOfReq, request: Request):
    return _run(request, "as_of", req.params())


# ---- LLM target resolver ----------------------------------------------------

@router.post("/resolve")
def resolve(req: ResolveReq, request: Request):
    """Natural-language instruction ("forget the beer stuff") → plan {intent, targets, new_fact,
    confidence, explanation, requires_confirmation}. Nothing is applied."""
    return _run(request, "resolve", req.params())


@router.post("/resolve/apply")
def resolve_apply(req: ResolveApplyReq, request: Request):
    """Apply a plan from /resolve, or pass `text` (+ `confirm: true` to apply even when the plan asks
    for confirmation)."""
    return _run(request, "resolve_apply", req.params())


# ---- episodes ---------------------------------------------------------------

@router.get("/episodes")
def episodes_list(request: Request, user_id: str | None = None, session_id: str | None = None,
                  kind: str | None = None, status: str | None = "active",
                  limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0)):
    return _run(request, "episodes_list", user_id=user_id, session_id=session_id, kind=kind, status=status,
                limit=limit, offset=offset)


@router.get("/episodes/{episode_id}")
def episode_get(episode_id: str, request: Request, user_id: str | None = None):
    return _run(request, "episode_get", episode_id=episode_id, user_id=user_id)


@router.delete("/episodes/{episode_id}")
def episode_delete(episode_id: str, request: Request, user_id: str | None = None):
    return _run(request, "episode_delete", episode_id=episode_id, user_id=user_id)


# ---- predicates / audit -----------------------------------------------------

@router.get("/predicates")
def predicates_list(request: Request):
    return _run(request, "predicates_list")


@router.patch("/predicates/{name}")
def predicate_update(name: str, req: PredicatePatch, request: Request):
    p = req.params()
    p["name"] = name
    return _run(request, "predicate_update", p)


@router.get("/audit")
def audit(request: Request, user_id: str | None = None, limit: int = Query(50, ge=1, le=1000),
          offset: int = Query(0, ge=0)):
    return _run(request, "audit", user_id=user_id, limit=limit, offset=offset)


# ---- graph layer + aliases --------------------------------------------------

@router.get("/graph")
def graph(request: Request, node: str, user_id: str | None = None, depth: int | None = Query(None, ge=0, le=6),
          fanout: int | None = Query(None, ge=1, le=200)):
    return _run(request, "graph", user_id=user_id, node=node, depth=depth, fanout=fanout)


@router.get("/edges")
def edges_list(request: Request, user_id: str | None = None, node: str | None = None, relation: str | None = None,
               depth: int = Query(0, ge=0, le=6), status: str | None = "active",
               limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0)):
    return _run(request, "edges_list", user_id=user_id, node=node, relation=relation, depth=depth, status=status,
                limit=limit, offset=offset)


@router.post("/edges")
def edge_add(req: EdgeReq, request: Request):
    return _run(request, "edge_add", req.params())


@router.delete("/edges/{edge_id}")
def edge_delete(edge_id: str, request: Request, user_id: str | None = None, mode: str = "retract"):
    return _run(request, "edge_delete", edge_id=edge_id, user_id=user_id, mode=mode)


@router.get("/aliases")
def aliases_list(request: Request, user_id: str | None = None, canonical: str | None = None,
                 limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0)):
    return _run(request, "aliases_list", user_id=user_id, canonical=canonical, limit=limit, offset=offset)


@router.post("/aliases")
def alias_add(req: AliasReq, request: Request):
    return _run(request, "alias_add", req.params())


@router.delete("/aliases/{alias}")
def alias_delete(alias: str, request: Request, user_id: str | None = None):
    return _run(request, "alias_delete", alias=alias, user_id=user_id)


# ---- dispatcher + admin -----------------------------------------------------

@router.post("/op")
def op(body: dict, request: Request):
    """Full access to every action for scripts: POST {"action": "...", ...params}."""
    body = dict(body or {})
    action = str(body.pop("action", "") or "")
    return _run(request, action, body)


@router.delete("/users/{user_id}")
def user_wipe(user_id: str, request: Request):
    return _run(request, "user_wipe", user_id=user_id)


# ---- MemoryOS compat --------------------------------------------------------

@router.post("/retrieve")
def compat_retrieve(req: RetrieveReq, request: Request):
    return _run(request, "retrieve", req.params())


@router.post("/memories")
def compat_memories(req: MemoriesReq, request: Request):
    return _run(request, "memories_add", req.params())


@router.get("/users/{user_id}/profile")
def compat_profile(user_id: str, request: Request):
    return _run(request, "user_profile", user_id=user_id)
