"""Rich rendering for the Astoria CLI — tables, panels, timelines.

Everything here is pure presentation: dicts in, console output out. `--json` mode bypasses
this module entirely (main.py prints the raw payload).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

console = Console()
err_console = Console(stderr=True)

LAYER_STYLE = {"profile": "magenta", "semantic": "cyan", "procedural": "yellow",
               "episodic": "green", "working": "dim"}
STATUS_STYLE = {"active": "green", "staging": "yellow", "superseded": "dim",
                "retracted": "red", "deleted": "red dim", "archived": "dim"}
KIND_STYLE = {"turn": "dim", "summary": "green", "note": "cyan", "import": "yellow"}


# ------------------------------------------------------------------ primitives
def short_id(v: Any, n: int = 8) -> str:
    return "" if v is None else str(v)[:n]


def fmt_dt(v: Any, date_only: bool = False) -> str:
    """ISO string → 'YYYY-MM-DD HH:MM' (local-agnostic: shows as given, trimmed)."""
    if not v:
        return ""
    s = str(v)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")
    except ValueError:
        return s[:10] if date_only else s[:16]


def fmt_conf(v: Any) -> Text:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return Text("")
    style = "green" if f >= 0.8 else "yellow" if f >= 0.5 else "red"
    return Text(f"{f:.2f}", style=style)


def fmt_num(v: Any, nd: int = 2) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "" if v is None else str(v)


def styled(value: str | None, styles: dict[str, str]) -> Text:
    v = value or ""
    return Text(v, style=styles.get(v, ""))


def trunc(s: Any, n: int = 60) -> str:
    s = "" if s is None else str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def flag(b: Any, yes: str = "✓", no: str = "") -> str:
    return yes if b else no


def valid_window(row: dict) -> str:
    vf, vt = row.get("valid_from"), row.get("valid_to")
    if not vf and not vt:
        return ""
    return f"{fmt_dt(vf, True) or '…'} → {fmt_dt(vt, True) or 'now'}"


def print_json(obj: Any) -> None:
    console.print_json(json.dumps(obj, default=str))


def info(msg: str) -> None:
    console.print(f"[dim]{msg}[/dim]")


def ok(msg: str) -> None:
    console.print(f"[green]✓[/green] {msg}")


def warn(msg: str) -> None:
    err_console.print(f"[yellow]![/yellow] {msg}")


def error(msg: str) -> None:
    err_console.print(f"[bold red]error:[/bold red] {msg}")


def empty(what: str) -> None:
    console.print(f"[dim](no {what})[/dim]")


# ---------------------------------------------------------------------- health
def health_table(h: dict) -> Table:
    t = Table(title="Astoria service", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("component", style="bold")
    t.add_column("state")
    t.add_column("detail", overflow="fold")

    status = h.get("status", "?")
    t.add_row("service", _state(status == "ok"),
              f"version {h.get('version', '?')}" if h.get("version") else "")

    db = h.get("db") or {}
    if isinstance(db, dict):
        db_ok = db.get("ok", db.get("status") in ("ok", True) or bool(db) and "error" not in db)
        t.add_row("db", _state(bool(db_ok)), _kv(db, skip=("ok", "status")))
    else:
        t.add_row("db", _state(bool(db)), str(db))

    tei = h.get("tei") or {}
    if isinstance(tei, dict):
        t.add_row("tei (embeddings)", _state(bool(tei.get("ok"))), _kv(tei, skip=("ok",)))
    else:
        t.add_row("tei (embeddings)", _state(bool(tei)), str(tei))

    llm = h.get("llm") or {}
    if isinstance(llm, dict):
        t.add_row("llm (cognify)", _state(bool(llm.get("saint") or llm.get("fallback"))),
                  _kv(llm))
    else:
        t.add_row("llm (cognify)", _state(bool(llm)), str(llm))

    q = h.get("queue") or {}
    if isinstance(q, dict):
        pending, dead = q.get("pending", 0), q.get("dead", 0)
        qstate = "[green]idle[/green]" if not pending and not dead else \
            ("[red]dead jobs[/red]" if dead else "[yellow]busy[/yellow]")
        t.add_row("cognify queue", qstate, _kv(q))
    else:
        t.add_row("cognify queue", "", str(q))

    known = {"status", "version", "db", "tei", "llm", "queue"}
    for k, v in h.items():
        if k not in known:
            t.add_row(k, "", _kv(v) if isinstance(v, dict) else str(v))
    return t


def queue_table(q: dict, source: str) -> Table:
    t = Table(title=f"cognify queue  [dim]({escape(source)})[/dim]", box=box.SIMPLE_HEAVY)
    t.add_column("metric", style="bold")
    t.add_column("value", justify="right")
    for k, v in q.items():
        style = ""
        if k == "dead" and v:
            style = "red"
        elif k == "pending" and v:
            style = "yellow"
        t.add_row(k, Text(str(v), style=style))
    return t


def _state(ok_: bool) -> str:
    return "[green]ok[/green]" if ok_ else "[red]down[/red]"


def _kv(d: Any, skip: Iterable[str] = ()) -> str:
    return escape(_kv_raw(d, skip))


def _kv_raw(d: Any, skip: Iterable[str] = ()) -> str:
    if not isinstance(d, dict):
        return "" if d is None else str(d)
    parts = []
    for k, v in d.items():
        if k in skip:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)
        parts.append(f"{k}={v}")
    return "  ".join(parts)


# ---------------------------------------------------------------------- recall
def context_panel(context: str, title: str = "context") -> Panel:
    body = Text(context.strip()) if context and context.strip() else \
        Text("(empty — nothing relevant in memory)", style="dim")
    return Panel(body, title=title, border_style="blue", expand=False, padding=(0, 1))


def recall_items_table(items: list[dict]) -> Table:
    t = Table(title=f"items ({len(items)})", box=box.SIMPLE_HEAVY)
    t.add_column("#", justify="right", style="dim", no_wrap=True)
    t.add_column("score", justify="right", no_wrap=True)
    t.add_column("conf", justify="right", no_wrap=True)
    t.add_column("layer", no_wrap=True)
    t.add_column("text", overflow="fold", min_width=20, max_width=80)
    t.add_column("source", max_width=12)
    t.add_column("stale?", no_wrap=True)
    t.add_column("id", style="dim", no_wrap=True)
    for i, it in enumerate(items, 1):
        text = it.get("text") or " ".join(
            str(it.get(k, "")) for k in ("subject", "predicate", "value")).strip()
        txt = Text(text, style="italic" if it.get("is_belief") else "")
        if it.get("is_belief"):
            txt.append(" (belief)", style="dim")
        stale = it.get("stale_hint")
        t.add_row(str(i), fmt_num(it.get("score"), 3), fmt_conf(it.get("confidence")),
                  styled(it.get("layer"), LAYER_STYLE), txt, str(it.get("source") or ""),
                  Text("stale", style="yellow") if stale else "", short_id(it.get("id")))
    return t


def working_table(turns: list[dict]) -> Table:
    t = Table(title=f"working memory — last {len(turns)} turn(s)", box=box.SIMPLE)
    t.add_column("when", no_wrap=True, style="dim")
    t.add_column("user", overflow="fold", max_width=60)
    t.add_column("agent", overflow="fold", max_width=60)
    for w in turns:
        t.add_row(fmt_dt(w.get("occurred_at")), Text(trunc(w.get("user_input"), 160)),
                  Text(trunc(w.get("agent_response"), 160)))
    return t


# ----------------------------------------------------------------------- facts
FACT_COLUMNS = ("id", "subject", "predicate", "value", "layer", "conf", "trust", "source",
                "asserted_at", "status")


def facts_table(rows: list[dict], title: str = "facts", show_status: bool = True) -> Table:
    t = Table(title=f"{escape(title)} ({len(rows)})", box=box.SIMPLE_HEAVY)
    t.add_column("id", style="dim", no_wrap=True)
    t.add_column("subject", min_width=6, max_width=16, overflow="fold")
    t.add_column("predicate", min_width=14, max_width=22, overflow="fold")
    t.add_column("value", min_width=12, max_width=48, overflow="fold")
    t.add_column("layer", no_wrap=True)
    t.add_column("conf", justify="right", no_wrap=True)
    t.add_column("trust", justify="right", no_wrap=True)
    t.add_column("source", max_width=12)
    t.add_column("asserted", no_wrap=True)
    if show_status:
        t.add_column("status", no_wrap=True)
    for r in rows:
        val = Text(str(r.get("value") or ""), style="italic" if r.get("is_belief") else "")
        cells = [short_id(r.get("id")), str(r.get("subject") or ""), str(r.get("predicate") or ""),
                 val, styled(r.get("layer"), LAYER_STYLE), fmt_conf(r.get("confidence")),
                 fmt_num(r.get("source_trust")), str(r.get("source") or ""),
                 fmt_dt(r.get("asserted_at"), True)]
        if show_status:
            cells.append(styled(r.get("status"), STATUS_STYLE))
        t.add_row(*cells)
    return t


def fact_detail(f: dict) -> Group:
    head = Text()
    head.append(f"{f.get('subject')} ", style="bold")
    head.append(f"{f.get('predicate')} ", style="bold cyan")
    head.append(f"{f.get('value')}", style="bold white")
    status = f.get("status", "")
    head.append(f"   [{status}]", style=STATUS_STYLE.get(status, ""))

    t = Table(box=box.SIMPLE, show_header=False, expand=False, padding=(0, 1))
    t.add_column("field", style="dim", width=16)
    t.add_column("value", overflow="fold")

    def row(k: str, v: Any, style: str = "") -> None:
        if v is None or v == "" or v == [] or v == {}:
            return
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)
        t.add_row(k, Text(str(v), style=style) if style else str(v))

    row("id", f.get("id"))
    row("user", f.get("user_id"))
    row("layer", f.get("layer"), LAYER_STYLE.get(str(f.get("layer")), ""))
    row("cardinality", f.get("cardinality"))
    row("is_belief", "yes — inference, not stated" if f.get("is_belief") else None)
    row("confidence", fmt_num(f.get("confidence")))
    row("source_trust", fmt_num(f.get("source_trust")))
    row("importance", fmt_num(f.get("importance")))
    row("corroborations", f.get("corroborations") or None)
    row("source", f"{f.get('source')} ({f.get('source_kind')})")
    row("valid", valid_window(f) or None)
    row("asserted_at", fmt_dt(f.get("asserted_at")))
    row("ingested_at", fmt_dt(f.get("ingested_at")))
    row("expired_at", fmt_dt(f.get("expired_at")))
    row("last_seen", fmt_dt(f.get("last_seen")))
    row("access_count", f.get("access_count") or None)
    row("origin_episode", f.get("origin_episode"))
    row("evidence", f.get("evidence"))
    row("ref", f.get("ref"))
    row("tags", ", ".join(f.get("tags") or []) or None)
    row("supersedes", f.get("supersedes"))
    row("superseded_by", f.get("superseded_by"))
    row("detail", f.get("detail"))
    meta = f.get("meta")
    if meta:
        row("meta", meta)
    return Group(head, t)


def history_timeline(rows: list[dict], subject: str, predicate: str) -> Tree:
    """Supersede chain newest-first from the API → rendered oldest→newest."""
    tree = Tree(f"[bold]{subject}[/bold] [cyan]{predicate}[/cyan] — {len(rows)} assertion(s)")
    for r in reversed(rows):
        status = str(r.get("status") or "")
        marker = {"active": "[green]●[/green]", "superseded": "[dim]○[/dim]",
                  "retracted": "[red]✗[/red]", "staging": "[yellow]◐[/yellow]"}.get(status, "·")
        label = Text.assemble(
            (fmt_dt(r.get("asserted_at")), "dim"), "  ",
            (str(r.get("value") or ""), "bold" if status == "active" else ""),
            ("  ", ""), (f"[{status}]", STATUS_STYLE.get(status, "")),
        )
        node = tree.add(Text.assemble(Text.from_markup(marker), " ", label))
        sub = []
        vw = valid_window(r)
        if vw:
            sub.append(f"valid {vw}")
        sub.append(f"conf {fmt_num(r.get('confidence'))} · {r.get('source')}/{r.get('source_kind')}")
        sub.append(f"id {short_id(r.get('id'))}")
        if r.get("supersedes"):
            sub.append(f"supersedes {short_id(r.get('supersedes'))}")
        if r.get("evidence"):
            sub.append(f"evidence: {trunc(r.get('evidence'), 100)}")
        node.add(Text("  ·  ".join(sub), style="dim"))
    return tree


# -------------------------------------------------------------------- episodes
def episodes_table(rows: list[dict]) -> Table:
    t = Table(title=f"episodes ({len(rows)})", box=box.SIMPLE_HEAVY)
    t.add_column("id", style="dim", no_wrap=True)
    t.add_column("kind", no_wrap=True)
    t.add_column("occurred", no_wrap=True)
    t.add_column("session", max_width=16, overflow="ellipsis")
    t.add_column("source", max_width=12)
    t.add_column("text", overflow="fold", min_width=20, max_width=80)
    t.add_column("cognified", no_wrap=True)
    for r in rows:
        text = r.get("hook") or r.get("text") or r.get("body") or ""
        t.add_row(short_id(r.get("id")), styled(r.get("kind"), KIND_STYLE),
                  fmt_dt(r.get("occurred_at")), str(r.get("session_id") or ""),
                  str(r.get("source") or ""), Text(trunc(text, 140)),
                  flag(r.get("processed_at"), "✓", "[dim]pending[/dim]"))
    return t


# ------------------------------------------------------------------ predicates
def predicates_table(rows: list[dict]) -> Table:
    t = Table(title=f"predicates ({len(rows)})", box=box.SIMPLE_HEAVY)
    t.add_column("name", style="bold")
    t.add_column("cardinality")
    t.add_column("layer_hint")
    t.add_column("auto", justify="center")
    t.add_column("description", overflow="fold")
    t.add_column("created", style="dim")
    for r in rows:
        card = str(r.get("cardinality") or "")
        t.add_row(str(r.get("name")), Text(card, style="magenta" if card == "functional" else ""),
                  styled(r.get("layer_hint"), LAYER_STYLE),
                  Text("auto", style="yellow") if r.get("auto") else "",
                  Text(str(r.get("description") or "")), fmt_dt(r.get("created_at"), True))
    return t


# --------------------------------------------------------------------- generic
def generic_table(rows: list[dict], title: str, prefer: tuple[str, ...] = (),
                  max_width: int = 60) -> Table:
    """Table for endpoints with loosely specified rows (audit, op results)."""
    cols: list[str] = [c for c in prefer if any(c in r for r in rows)]
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    t = Table(title=f"{escape(title)} ({len(rows)})", box=box.SIMPLE_HEAVY)
    for c in cols:
        t.add_column(c, overflow="fold", max_width=max_width)
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, (dict, list)):
                v = json.dumps(v, default=str)
            elif c.endswith("_at") or c in ("at", "ts", "time", "created", "when"):
                v = fmt_dt(v)
            elif c == "id" or c.endswith("_id"):
                v = short_id(v, 12)
            cells.append(Text(trunc(v, max_width * 2)))
        t.add_row(*cells)
    return t


def profile_panel(narrative: str, version: Any = None) -> Panel:
    title = "profile narrative" + (f" (v{version})" if version is not None else "")
    body = Text(narrative.strip()) if narrative and narrative.strip() else \
        Text("(no narrative yet — profile facts below)", style="dim")
    return Panel(body, title=title, border_style="magenta", expand=False, padding=(0, 1))


def code_block(text: str, lexer: str = "json") -> Syntax:
    return Syntax(text, lexer, theme="ansi_dark", word_wrap=True)


# ----------------------------------------------------------------------- graph
def _node_text(n: dict) -> Text:
    kind = str(n.get("kind") or "")
    name = str(n.get("name") or n.get("id") or "")
    if kind == "fact":
        t = Text.assemble(("fact ", "dim"), (short_id(name), "dim"), " ", (str(n.get("label") or ""), "cyan"))
    else:
        t = Text.assemble((name, "bold"))
        if n.get("entity_kind"):
            t.append(f" ({n['entity_kind']})", style="dim")
        if n.get("aliases"):
            t.append("  aka " + ", ".join(n["aliases"]), style="dim")
        if n.get("facts") is not None:
            t.append(f"  [{n['facts']} facts]", style="dim")
    return t


def graph_tree(g: dict) -> Tree:
    """/graph payload → rich tree: root, then children by path (hops)."""
    nodes = g.get("nodes") or []
    edges = g.get("edges") or []
    by_id = {n["id"]: n for n in nodes}
    root_id = g.get("root")
    root = by_id.get(root_id) or {"id": root_id, "kind": "entity", "name": root_id}
    title = _node_text(root)
    title.append(f"   depth {g.get('depth')} · {len(nodes)} nodes · {len(edges)} edges", style="dim")
    tree = Tree(title)
    branches: dict[str, Tree] = {root_id: tree}
    for n in sorted(nodes, key=lambda x: (int(x.get("hops") or 0), str(x.get("id")))):
        if n["id"] == root_id:
            continue
        path = n.get("path") or []
        parent_id = path[-2] if len(path) >= 2 else root_id
        parent = branches.get(parent_id, tree)
        arrow = "→" if n.get("direction") == "out" else "←"
        label = Text.assemble((f"{arrow} {n.get('via') or ''} ", "magenta"))
        label.append_text(_node_text(n))
        branches[n["id"]] = parent.add(label)
    return tree


def edges_table(rows: list[dict], title: str = "edges") -> Table:
    """src —relation→ dst per row; weight shown as ×w when ≠ 1; provenance in --json / `graph`."""
    t = Table(title=f"{title} ({len(rows)})", box=box.SIMPLE_HEAVY, expand=False, pad_edge=False)
    t.add_column("id", style="dim", no_wrap=True)
    t.add_column("src", no_wrap=True, overflow="ellipsis")
    t.add_column("relation", style="magenta", no_wrap=True)
    t.add_column("dst", no_wrap=True, overflow="ellipsis")
    t.add_column("conf", justify="right", no_wrap=True)
    t.add_column("status", no_wrap=True)
    t.add_column("src kind", no_wrap=True, overflow="ellipsis")
    for r in rows:
        st = str(r.get("status") or "")
        w = r.get("weight")
        wtxt = f" ×{fmt_num(w, 1)}" if w not in (None, 1, 1.0) else ""
        t.add_row(short_id(r.get("id")), _edge_end(r, "src"), f"{r.get('relation') or ''}{wtxt}", _edge_end(r, "dst"),
                  fmt_conf(r.get("confidence")), Text(st, style=STATUS_STYLE.get(st, "")),
                  str(r.get("source_kind") or ""))
    return t


def _edge_end(r: dict, side: str) -> str:
    kind, nid = r.get(f"{side}_kind"), str(r.get(f"{side}_id") or "")
    return f"fact:{short_id(nid)}" if kind == "fact" else nid


def aliases_table(rows: list[dict]) -> Table:
    t = Table(title=f"aliases ({len(rows)})", box=box.SIMPLE_HEAVY)
    t.add_column("alias", style="bold")
    t.add_column("→", no_wrap=True)
    t.add_column("canonical", style="cyan")
    t.add_column("source", no_wrap=True)
    t.add_column("created", no_wrap=True)
    for r in rows:
        t.add_row(str(r.get("alias") or ""), "→", str(r.get("canonical") or ""),
                  f"{r.get('source') or ''}/{r.get('source_kind') or ''}", fmt_dt(r.get("created_at"), True))
    return t
