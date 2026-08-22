"""`astoria` — command-line client for the Astoria memory service.

A thin HTTP client over the REST contract in docs/CONTRACT.md. It never touches the
database; every command is one (occasionally two) HTTP calls to the service.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from dateutil import parser as dateparser
from rich.console import Group
from rich.markup import escape
from rich.text import Text

from astoria.cli import render as R
from astoria.cli.client import (EXIT_ERROR, ApiError, AstoriaClient, env_token, env_url,
                                env_user)

# ============================================================================ app
HELP = """\
[bold]Astoria[/bold] — layered, trusted, deep memory for agents and humans. This CLI talks to the
Astoria service over HTTP (it never touches the database).

[bold]Layers[/bold]
  [dim]working[/dim]     raw turns of one session (per [cyan]--session[/cyan]) — prepended to recall, never searched
  [green]episodic[/green]    summaries / notes / imports — "what happened"
  [cyan]semantic[/cyan]    (subject, predicate, value) facts — "what is true"
  [magenta]profile[/magenta]     facts about the user that shape every answer (+ a narrative)
  [yellow]procedural[/yellow]  how-to knowledge linked to skills / plans / docs

[bold]Trust in two lines[/bold]
  Every fact carries [bold]confidence[/bold] (explicit .90 · detector .80 · extracted ≤.85 · imported .45) and a
  [bold]source_trust[/bold] capped by the client that wrote it; both only [italic]rank[/italic] — a newer human statement always wins.
  Low-confidence extractions land in [yellow]staging[/yellow] (not recalled) until you [cyan]approve[/cyan] them.

[bold]Common workflows[/bold]
  [cyan]astoria recall "what editor do I use"[/cyan]            → context block + ranked items
  [cyan]astoria remember alice favorite_beer IPA[/cyan]           → assert a fact (supersedes the old value)
  [cyan]astoria remember --text "Prefers dark mode"[/cyan]       → capture a note; the worker extracts facts
  [cyan]astoria correct alice favorite_beer Stout[/cyan]          → same as remember, shows what it replaced
  [cyan]astoria resolve "forget the beer stuff"[/cyan]            → LLM resolves WHICH facts are meant (preview only)
  [cyan]astoria forget "the thing about Guinness"[/cyan]         → resolve → show targets → confirm → apply
  [cyan]astoria facts -q beer[/cyan] · [cyan]astoria fact 3f2a[/cyan]        → browse, then inspect provenance
  [cyan]astoria history alice favorite_beer[/cyan]                → supersede chain as a timeline
  [cyan]astoria as-of 2026-01-01[/cyan]                          → what was true back then
  [cyan]astoria staging[/cyan] → [cyan]astoria approve ID[/cyan]               → review extracted facts
  [cyan]astoria briefing[/cyan] · [cyan]astoria profile[/cyan]                  → stable prompt prefix / who the user is
  [cyan]astoria graph buildbot[/cyan] · [cyan]astoria edge add A runs_on B[/cyan]   → walk / extend the entity graph
  [cyan]astoria alias add ws1 workstation-1[/cyan]       → two names, one subject

[bold]Environment[/bold]
  ASTORIA_URL    service base URL   (default http://localhost:8933)
  ASTORIA_TOKEN  bearer token → client name server-side (sent as Authorization: Bearer)
  ASTORIA_USER   default user_id   (default: empty → the server's ASTORIA_USER_DEFAULT)

Short fact ids (first 8 chars, as printed in tables) are accepted anywhere an ID is expected.
Dates accept YYYY-MM-DD, ISO-8601, or "now" / "today" / "yesterday" / "3 days ago" / "2 weeks ago".
"""

app = typer.Typer(
    name="astoria",
    help=HELP,
    rich_markup_mode="rich",
    no_args_is_help=True,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)
episode_app = typer.Typer(help="Operate on a single episode ([cyan]delete[/cyan]). "
                               "Use [cyan]astoria episodes[/cyan] to list.",
                          rich_markup_mode="rich", no_args_is_help=True)
predicate_app = typer.Typer(help="Operate on a single predicate ([cyan]set[/cyan] cardinality/layer). "
                                 "Use [cyan]astoria predicates[/cyan] to list.",
                            rich_markup_mode="rich", no_args_is_help=True)
app.add_typer(episode_app, name="episode", rich_help_panel="Episodes & capture")
app.add_typer(predicate_app, name="predicate", rich_help_panel="Admin")
edge_app = typer.Typer(help="Operate on a single edge ([cyan]add[/cyan] / [cyan]rm[/cyan]). "
                            "Use [cyan]astoria edges[/cyan] to list, [cyan]astoria graph NODE[/cyan] to walk.",
                       rich_markup_mode="rich", no_args_is_help=True)
alias_app = typer.Typer(help="Subject aliases: [cyan]add ALIAS CANONICAL[/cyan] · [cyan]list[/cyan] · [cyan]rm ALIAS[/cyan]. "
                             "Writes and reads on ALIAS land on CANONICAL.",
                        rich_markup_mode="rich", no_args_is_help=True)
app.add_typer(edge_app, name="edge", rich_help_panel="Graph & aliases")
app.add_typer(alias_app, name="alias", rich_help_panel="Graph & aliases")

P_READ = "Read memory"
P_WRITE = "Write memory"
P_FACTS = "Facts & time travel"
P_EPI = "Episodes & capture"
P_ADMIN = "Admin"
P_DATA = "Data"
P_GRAPH = "Graph & aliases"


@dataclass
class Ctx:
    client: AstoriaClient
    json: bool


def _ctx(ctx: typer.Context) -> Ctx:
    return ctx.obj


@app.callback()
def _root(
    ctx: typer.Context,
    user: str = typer.Option(env_user(), "--user", "-u", envvar="ASTORIA_USER",
                             help="user_id every request is scoped to.", show_default=True),
    url: str = typer.Option(env_url(), "--url", envvar="ASTORIA_URL",
                            help="Service base URL.", show_default=True),
    token: Optional[str] = typer.Option(env_token(), "--token", envvar="ASTORIA_TOKEN",
                                        help="Bearer token (maps to a client name and trust cap "
                                             "server-side).", show_default=False),
    json_out: bool = typer.Option(False, "--json", "-j",
                                  help="Print the raw JSON response instead of tables "
                                       "(scripting / jq).", is_flag=True),
    timeout: float = typer.Option(30.0, "--timeout", help="HTTP timeout in seconds."),
):
    """Global options apply to every sub-command: [cyan]astoria --user bob --json facts[/cyan]."""
    ctx.obj = Ctx(client=AstoriaClient(base_url=url.rstrip("/"), token=token, user=user,
                                       timeout=timeout), json=json_out)


def main() -> None:  # console_scripts entry also works via `app` directly
    try:
        app(standalone_mode=True)
    except ApiError as e:  # pragma: no cover - typer catches earlier via _run
        R.error(str(e))
        sys.exit(e.exit_code)


# ============================================================ shared helpers
_REL = re.compile(r"^(\d+)\s*(minute|min|hour|hr|day|week|month|year)s?\s+ago$", re.I)


def parse_date(s: str | None) -> str | None:
    """Friendly date → ISO-8601 (UTC). None passes through."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    now = datetime.now(timezone.utc)
    low = s.lower()
    if low == "now":
        return now.isoformat()
    if low == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if low == "yesterday":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                 microsecond=0).isoformat()
    if low == "tomorrow":
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                 microsecond=0).isoformat()
    m = _REL.match(low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        unit = {"min": "minute", "hr": "hour"}.get(unit, unit)
        if unit == "month":
            delta = timedelta(days=30 * n)
        elif unit == "year":
            delta = timedelta(days=365 * n)
        else:
            delta = timedelta(**{unit + "s": n})
        return (now - delta).isoformat()
    try:
        dt = dateparser.parse(s)
    except (ValueError, OverflowError) as e:
        raise typer.BadParameter(f"cannot parse date '{s}': {e}") from e
    if dt is None:
        raise typer.BadParameter(f"cannot parse date '{s}'")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _run(ctx: typer.Context, fn, *args, **kwargs):
    """Call an API function; translate ApiError into a one-line message + exit code."""
    try:
        return fn(*args, **kwargs)
    except ApiError as e:
        R.error(str(e))
        if _ctx(ctx).json and e.body is not None:
            R.print_json(e.body)
        raise typer.Exit(code=e.exit_code)


def _emit(ctx: typer.Context, payload: Any, renderable=None) -> None:
    """--json → raw payload; else the rich renderable (or pretty JSON if none given)."""
    if _ctx(ctx).json or renderable is None:
        R.print_json(payload)
    else:
        R.console.print(renderable)


def _fact_id(ctx: typer.Context, ident: str) -> str:
    return _run(ctx, _ctx(ctx).client.resolve_fact_id, ident)


def _fact_line(f: dict) -> str:
    return (f"[dim]{R.short_id(f.get('id'))}[/dim] [bold]{escape(str(f.get('subject')))}[/bold] "
            f"[cyan]{escape(str(f.get('predicate')))}[/cyan] {escape(str(f.get('value')))} "
            f"[dim]({f.get('layer')} · conf {R.fmt_num(f.get('confidence'))} · "
            f"{f.get('status')})[/dim]")


def _split_csv(v: Optional[str]) -> list[str] | None:
    if not v:
        return None
    return [x.strip() for x in v.split(",") if x.strip()]


# ================================================================ Read memory
@app.command(rich_help_panel=P_READ)
def status(ctx: typer.Context):
    """Service health — db / tei (embeddings) / llm (cognify) / queue — as a table.

    Exit code is 0 only when the service reports [green]ok[/green].

    [dim]Examples:[/dim]  [cyan]astoria status[/cyan]   ·   [cyan]astoria --json status | jq .queue[/cyan]
    """
    c = _ctx(ctx).client
    h = _run(ctx, c.health)
    _emit(ctx, h, R.health_table(h))
    if (h or {}).get("status") != "ok":
        raise typer.Exit(code=EXIT_ERROR)


@app.command(rich_help_panel=P_READ, hidden=False)
def health(ctx: typer.Context):
    """Alias of [cyan]status[/cyan]."""
    status(ctx)


@app.command(rich_help_panel=P_READ)
def recall(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Natural-language query (hybrid vector + BM25 search)."),
    layers: Optional[str] = typer.Option(
        None, "--layers", "-L",
        help="Comma list of layers to search: profile,semantic,procedural,episodic "
             "(default: all four)."),
    limit: int = typer.Option(12, "--limit", "-n", help="Max items after collapse."),
    tokens: int = typer.Option(1000, "--tokens", "-t",
                               help="Token budget for the rendered context block (~chars/4)."),
    facts_only: bool = typer.Option(False, "--facts-only", help="Skip episodes; facts only."),
    as_of: Optional[str] = typer.Option(
        None, "--as-of", help="Time-travel: facts valid at this date (YYYY-MM-DD / ISO / "
                              "'2 weeks ago')."),
    believed_at: Optional[str] = typer.Option(
        None, "--believed-at", help="Also restrict to what the system believed at this time."),
    session: Optional[str] = typer.Option(
        None, "--session", "-s", help="Session id — prepends the last 4 turns as working memory."),
    profile: bool = typer.Option(False, "--profile", help="Include the profile block "
                                                           "(narrative + profile facts)."),
    context_only: bool = typer.Option(False, "--context-only",
                                      help="Print just the context block (pipe into a prompt)."),
):
    """Recall memory for a query: prints the ready-to-inject [bold]context[/bold] block and a ranked
    items table (score · conf · layer · text · stale?).

    [dim]Examples:[/dim]
      [cyan]astoria recall "favorite beer"[/cyan]
      [cyan]astoria recall "deploy steps" --layers procedural,semantic --facts-only[/cyan]
      [cyan]astoria recall "editor" --as-of 2026-01-01[/cyan]
      [cyan]astoria recall "what were we doing" --session abc123 --profile[/cyan]
      [cyan]astoria recall "beer" --context-only >> prompt.txt[/cyan]
    """
    c = _ctx(ctx).client
    res = _run(ctx, c.recall, query, session_id=session, layers=_split_csv(layers),
               max_tokens=tokens, limit=limit, facts_only=facts_only, include_profile=profile,
               as_of=parse_date(as_of), as_believed_at=parse_date(believed_at))
    if _ctx(ctx).json:
        R.print_json(res)
        return
    if context_only:
        print(res.get("context") or "")
        return
    parts: list = []
    health = res.get("health") or {}
    if health.get("degraded"):
        R.warn("degraded: embeddings unavailable — BM25 only")
    working = res.get("working") or []
    if working:
        parts.append(R.working_table(working))
    prof = res.get("profile")
    if prof:
        parts.append(R.profile_panel(prof.get("narrative") or "", prof.get("version")))
    parts.append(R.context_panel(res.get("context") or ""))
    items = res.get("items") or []
    if items:
        parts.append(R.recall_items_table(items))
    else:
        parts.append(Text("(no items)", style="dim"))
    foot = f"snapshot {R.short_id(res.get('snapshot_id'))}" if res.get("snapshot_id") else ""
    if foot:
        parts.append(Text(foot, style="dim"))
    R.console.print(Group(*parts))


@app.command(rich_help_panel=P_READ)
def briefing(
    ctx: typer.Context,
    tokens: int = typer.Option(1200, "--tokens", "-t", help="Token budget."),
    raw: bool = typer.Option(False, "--raw", help="Print only the context text (no panel)."),
):
    """The stable briefing block — narrative + top profile facts — designed as a cacheable
    prompt prefix.

    [dim]Examples:[/dim]  [cyan]astoria briefing[/cyan]   ·   [cyan]astoria briefing --raw > system_prefix.txt[/cyan]
    """
    c = _ctx(ctx).client
    res = _run(ctx, c.briefing, tokens)
    if _ctx(ctx).json:
        R.print_json(res)
        return
    if raw:
        print(res.get("context") or "")
        return
    parts: list = []
    if res.get("narrative"):
        parts.append(R.profile_panel(res["narrative"]))
    parts.append(R.context_panel(res.get("context") or "", title="briefing"))
    facts = res.get("facts") or []
    if facts:
        parts.append(R.facts_table(facts, title="briefing facts", show_status=False))
    R.console.print(Group(*parts))


@app.command(rich_help_panel=P_READ)
def profile(ctx: typer.Context):
    """Who the user is: the profile narrative plus every profile-layer fact.

    [dim]Example:[/dim]  [cyan]astoria --user bob profile[/cyan]
    """
    c = _ctx(ctx).client
    res = _run(ctx, c.profile)
    facts = res.get("facts") or []
    _emit(ctx, res, Group(R.profile_panel(res.get("narrative") or "", res.get("version")),
                          R.facts_table(facts, title="profile facts", show_status=False)
                          if facts else Text("(no profile facts)", style="dim")))


# =============================================================== Write memory
def _remember_impl(ctx: typer.Context, subject, predicate, value, *, valid_from, valid_to,
                   functional, layer, confidence, tags, historical, endpoint="facts") -> dict:
    c = _ctx(ctx).client
    cardinality = None
    if functional is True:
        cardinality = "functional"
    elif functional is False:
        cardinality = "set"
    if endpoint == "correct":
        res = _run(ctx, c.correct, subject, predicate, value, valid_from=parse_date(valid_from))
    else:
        res = _run(ctx, c.add_fact, subject, predicate, value, valid_from=parse_date(valid_from),
                   valid_to=parse_date(valid_to), cardinality=cardinality, layer=layer,
                   confidence=confidence, tags=_split_csv(tags),
                   historical=True if historical else None)
    return res


def _print_upsert(ctx: typer.Context, res: dict, verb: str) -> None:
    if _ctx(ctx).json:
        R.print_json(res)
        return
    fact = res.get("fact")
    action = res.get("action", "")
    if fact:
        R.ok(f"{verb} [{action or 'ok'}] " + _fact_line(fact))
    else:
        R.ok(f"{verb}: {action or 'no change'}")
    superseded = res.get("superseded") or []
    if superseded:
        c = _ctx(ctx).client
        for sid in superseded:
            try:
                old = c.get_fact(str(sid))
                R.console.print(f"  [dim]superseded →[/dim] {_fact_line(old)}")
            except ApiError:
                R.console.print(f"  [dim]superseded → {sid}[/dim]")


@app.command(rich_help_panel=P_WRITE)
def remember(
    ctx: typer.Context,
    subject: Optional[str] = typer.Argument(None, help="Subject ('alice' / 'I' / 'me' → the user)."),
    predicate: Optional[str] = typer.Argument(None, help="snake_case predicate, e.g. favorite_beer."),
    value: Optional[str] = typer.Argument(None, help="Value text."),
    text: Optional[str] = typer.Option(
        None, "--text", help="Free-text mode: capture a [bold]note[/bold] episode instead of a "
                             "triple; the cognify worker extracts facts from it."),
    valid_from: Optional[str] = typer.Option(None, "--from",
                                             help="Valid-from date (real-world time)."),
    valid_to: Optional[str] = typer.Option(None, "--to", help="Valid-to date (closes the window)."),
    functional: Optional[bool] = typer.Option(
        None, "--functional/--set",
        help="Cardinality override: [bold]functional[/bold] = one current value (replaces), "
             "[bold]set[/bold] = many values (adds). Default: predicate registry / heuristic."),
    layer: Optional[str] = typer.Option(None, "--layer",
                                        help="semantic | profile | procedural (default: registry)."),
    confidence: Optional[float] = typer.Option(None, "--confidence",
                                               help="Override confidence (explicit default .90)."),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags."),
    historical: bool = typer.Option(False, "--historical",
                                    help="Record as a past value without disturbing the current one."),
    session: Optional[str] = typer.Option(None, "--session", "-s",
                                          help="(with --text) session id for the note."),
):
    """Assert a fact explicitly (confidence .90, your client's trust). A functional predicate
    supersedes the previous value; a set predicate adds another value.

    [dim]Examples:[/dim]
      [cyan]astoria remember alice favorite_beer IPA[/cyan]
      [cyan]astoria remember alice uses_tool Neovim --set[/cyan]
      [cyan]astoria remember alice lives_in Portland --from 2024-06-01[/cyan]
      [cyan]astoria remember alice employer Acme --from 2019-01-01 --to 2023-12-31 --historical[/cyan]
      [cyan]astoria remember --text "Rick prefers dark mode and tabs over spaces"[/cyan]
    """
    if text:
        if subject or predicate or value:
            raise typer.BadParameter("--text cannot be combined with SUBJECT PREDICATE VALUE")
        c = _ctx(ctx).client
        res = _run(ctx, c.capture, kind="note", text=text, session_id=session,
                   tags=_split_csv(tags))
        if _ctx(ctx).json:
            R.print_json(res)
        else:
            _print_capture(res)
        return
    if not (subject and predicate and value):
        raise typer.BadParameter("need SUBJECT PREDICATE VALUE (or --text \"...\")")
    res = _remember_impl(ctx, subject, predicate, value, valid_from=valid_from,
                         valid_to=valid_to, functional=functional, layer=layer,
                         confidence=confidence, tags=tags, historical=historical)
    _print_upsert(ctx, res, "remembered")


# ---- LLM target resolver (natural-language forget / correct / retract) --------------------
INTENT_STYLE = {"forget": "red", "retract": "yellow", "correct": "cyan", "remember": "green", "none": "dim"}


def _looks_like_free_text(arg: str | None, predicate: str | None) -> bool:
    """A single argument with no predicate that isn't a (short) fact id → natural-language instruction."""
    if not arg or predicate:
        return False
    a = arg.strip()
    if re.fullmatch(r"[0-9a-fA-F-]{8,36}", a):
        return False
    return True


def _print_plan(ctx: typer.Context, plan: dict) -> None:
    if _ctx(ctx).json:
        R.print_json(plan)
        return
    intent = str(plan.get("intent") or "none")
    style = INTENT_STYLE.get(intent, "")
    conf = plan.get("confidence")
    R.console.print(f"[bold]intent[/bold] [{style}]{escape(intent)}[/{style}]  "
                    f"[dim]confidence {R.fmt_num(conf)} · {plan.get('candidates', 0)} candidate(s) considered[/dim]")
    if plan.get("explanation"):
        R.console.print(f"  {escape(str(plan['explanation']))}")
    if plan.get("error"):
        R.warn(str(plan["error"]))
    targets = plan.get("targets") or []
    if targets:
        R.console.print(R.facts_table(targets, title=f"targets ({intent})"))
        for t in targets:
            if t.get("reason"):
                R.console.print(f"  [dim]{R.short_id(t.get('id'))}: {escape(str(t['reason']))}[/dim]")
    elif intent in ("forget", "retract", "correct"):
        R.info("  (no stored fact matched as a target)")
    nf = plan.get("new_fact")
    if nf:
        vf = f" [dim]from {nf.get('valid_from')}[/dim]" if nf.get("valid_from") else ""
        R.console.print(f"  [bold]new fact[/bold] [bold]{escape(str(nf.get('subject')))}[/bold] "
                        f"[cyan]{escape(str(nf.get('predicate')))}[/cyan] {escape(str(nf.get('value')))}{vf}")
    if intent != "none":
        R.console.print("  [dim]" + ("confirmation required" if plan.get("requires_confirmation")
                                      else "unambiguous — would apply without confirmation") + "[/dim]")


def _print_applied(ctx: typer.Context, res: dict) -> None:
    if _ctx(ctx).json:
        R.print_json(res)
        return
    if not res.get("applied"):
        R.warn(f"nothing applied ({res.get('reason') or res.get('action') or 'no change'})")
        return
    intent = res.get("intent")
    changed = res.get("changed") or []
    if intent in ("forget", "retract"):
        verb = "forgot" if intent == "forget" else "retracted"
        ids = [R.short_id((ch.get("fact") or {}).get("id")) for ch in changed]
        R.ok(f"{verb} {len(ids)} fact(s): " + ", ".join(ids))
    else:
        f = res.get("fact")
        R.ok(f"{'corrected' if intent == 'correct' else 'remembered'} [{res.get('action') or 'ok'}] "
             + (_fact_line(f) if f else ""))
        c = _ctx(ctx).client
        for sid in res.get("superseded") or []:
            try:
                R.console.print(f"  [dim]superseded →[/dim] {_fact_line(c.get_fact(str(sid)))}")
            except ApiError:
                R.console.print(f"  [dim]superseded → {sid}[/dim]")


def _resolve_flow(ctx: typer.Context, text: str, *, expect: str | None, yes: bool,
                  plan: dict | None = None) -> None:
    """resolve → preview → confirm → apply. `expect` = the sub-command's intent (forget/correct/retract);
    a different resolved intent is shown and needs an explicit confirmation (refused under --yes).
    `plan` = an already-fetched plan (skips the resolve call)."""
    c = _ctx(ctx).client
    if plan is None:
        plan = _run(ctx, c.resolve, text)
    _print_plan(ctx, plan)
    intent = str(plan.get("intent") or "none")
    if intent == "none" or plan.get("error"):
        if not _ctx(ctx).json:
            R.warn("no memory operation to apply")
        raise typer.Exit(code=1)
    if intent in ("forget", "retract", "correct") and not (plan.get("targets") or []) and intent != "correct":
        if not _ctx(ctx).json:
            R.warn("nothing to apply — no matching fact")
        raise typer.Exit(code=1)
    mismatch = expect is not None and intent != expect
    if yes:
        if mismatch:
            R.warn(f"resolver read this as '{intent}', not '{expect}' — re-run without --yes to confirm, "
                   f"or use `astoria {intent if intent != 'remember' else 'remember'} ...`")
            raise typer.Exit(code=1)
    else:
        what = {"forget": "soft-forget the target(s)", "retract": "retract the target(s)",
                "correct": "apply the correction", "remember": "remember the new fact"}[intent]
        prefix = f"(resolved as '{intent}', not '{expect}') " if mismatch else ""
        if not typer.confirm(f"{prefix}{what}?", default=False):
            raise typer.Exit(code=1)
    res = _run(ctx, c.resolve_apply, plan=plan, confirm=True)
    _print_applied(ctx, res)


@app.command(rich_help_panel=P_WRITE)
def resolve(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Natural-language memory instruction."),
    apply: bool = typer.Option(False, "--apply", help="Apply the plan (asks first unless --yes)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="With --apply: skip the confirmation prompt."),
):
    """Preview how the LLM target-resolver reads a natural-language instruction: which stored facts it
    means and what it would do (forget / retract / correct / remember). Nothing is applied unless
    [cyan]--apply[/cyan].

    [dim]Examples:[/dim]
      [cyan]astoria resolve "forget the beer stuff"[/cyan]
      [cyan]astoria resolve "actually I moved to Oakland" --apply[/cyan]
    """
    if apply:
        _resolve_flow(ctx, text, expect=None, yes=yes)
        return
    plan = _run(ctx, _ctx(ctx).client.resolve, text)
    _print_plan(ctx, plan)


@app.command(rich_help_panel=P_WRITE)
def correct(
    ctx: typer.Context,
    subject: str = typer.Argument(..., help="Subject — or a free-text correction "
                                            "(\"actually I live in Oakland\") when no PREDICATE follows."),
    predicate: Optional[str] = typer.Argument(None, help="Predicate."),
    value: Optional[str] = typer.Argument(None, help="The new, correct value."),
    valid_from: Optional[str] = typer.Option(None, "--from", help="When the new value became true."),
    yes: bool = typer.Option(False, "--yes", "-y", help="(free-text mode) skip the confirmation prompt."),
):
    """Correct a fact — same as [cyan]remember[/cyan] (POST /correct) but prints what it superseded.
    Given ONE free-text argument instead of a triple, the LLM resolver finds the fact to replace:
    preview → confirm → apply.

    [dim]Examples:[/dim]
      [cyan]astoria correct alice favorite_beer Stout[/cyan]
      [cyan]astoria correct "actually I live in Oakland"[/cyan]
    """
    if _looks_like_free_text(subject, predicate) and value is None:
        _resolve_flow(ctx, subject, expect="correct", yes=yes)
        return
    if not (predicate and value):
        raise typer.BadParameter("need SUBJECT PREDICATE VALUE (or one free-text \"...\" argument)")
    res = _remember_impl(ctx, subject, predicate, value, valid_from=valid_from, valid_to=None,
                         functional=None, layer=None, confidence=None, tags=None,
                         historical=False, endpoint="correct")
    _print_upsert(ctx, res, "corrected")
    if not _ctx(ctx).json and not (res.get("superseded") or []):
        R.info("  (nothing superseded — this was new or already the current value)")


@app.command(rich_help_panel=P_WRITE)
def retract(
    ctx: typer.Context,
    subject: Optional[str] = typer.Argument(None, help="Subject — or a free-text statement "
                                                       "(\"I don't use Emacs anymore\") when no PREDICATE follows."),
    predicate: Optional[str] = typer.Argument(None, help="Predicate."),
    value: Optional[str] = typer.Argument(None, help="Value (omit to retract every value of the key)."),
    fact_id: Optional[str] = typer.Option(None, "--id", help="Retract by fact id instead."),
    yes: bool = typer.Option(False, "--yes", "-y", help="(free-text mode) skip the confirmation prompt."),
):
    """Retract a fact: it stops being true [italic]now[/italic] (status → retracted, history kept,
    tombstoned so extraction can't resurrect it). One free-text argument → the LLM resolver picks
    the fact(s): preview → confirm → apply.

    [dim]Examples:[/dim]
      [cyan]astoria retract alice favorite_beer[/cyan]         (all values of the key)
      [cyan]astoria retract alice uses_tool Emacs[/cyan]       (one value)
      [cyan]astoria retract --id 3f2a9c1e[/cyan]
      [cyan]astoria retract "I don't use Emacs anymore"[/cyan]
    """
    c = _ctx(ctx).client
    if fact_id:
        fid = _fact_id(ctx, fact_id)
        res = _run(ctx, c.retract, fact_id=fid)
    elif subject and predicate:
        res = _run(ctx, c.retract, subject=subject, predicate=predicate, value=value)
    elif _looks_like_free_text(subject, predicate):
        _resolve_flow(ctx, subject, expect="retract", yes=yes)
        return
    else:
        raise typer.BadParameter("need SUBJECT PREDICATE [VALUE], --id ID, or one free-text \"...\" argument")
    if _ctx(ctx).json:
        R.print_json(res)
        return
    ids = res.get("retracted") or []
    if ids:
        R.ok(f"retracted {len(ids)} fact(s): " + ", ".join(R.short_id(i) for i in ids))
    else:
        R.warn("nothing matched — nothing retracted")


@app.command(rich_help_panel=P_WRITE)
def forget(
    ctx: typer.Context,
    query: Optional[str] = typer.Argument(None, help="Natural-language instruction (LLM-resolved) — "
                                                     "or, with --match, literal search text."),
    fact_id: Optional[str] = typer.Option(None, "--id", help="Forget exactly this fact id."),
    hard: bool = typer.Option(False, "--hard",
                              help="Hard delete (row removed). Default is soft (status=deleted, "
                                   "recoverable, tombstoned)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    match: bool = typer.Option(False, "--match", "-m",
                               help="Literal mode: substring-match QUERY against fact hooks "
                                    "(no LLM); lists the matches and asks before acting."),
    limit: int = typer.Option(25, "--limit", help="(--match mode) max matches to show/forget."),
):
    """Forget facts — stronger than retract: they vanish from history too (soft = hidden,
    --hard = gone). A free-text QUERY goes through the LLM target-resolver: it shows WHICH facts it
    thinks you mean, asks, then soft-forgets them ([cyan]--match[/cyan] = plain substring search instead;
    [cyan]--hard[/cyan] implies --match or --id).

    [dim]Examples:[/dim]
      [cyan]astoria forget --id 3f2a9c1e[/cyan]
      [cyan]astoria forget "the thing about Guinness"[/cyan]   (resolve → preview → confirm)
      [cyan]astoria forget "old address" --match[/cyan]        (substring preview → confirm)
      [cyan]astoria forget "old address" --match --hard --yes[/cyan]
    """
    c = _ctx(ctx).client
    mode = "hard" if hard else "soft"
    if fact_id:
        fid = _fact_id(ctx, fact_id)
        if not yes and not _ctx(ctx).json:
            try:
                f = c.get_fact(fid)
                R.console.print(_fact_line(f))
            except ApiError:
                pass
            if not typer.confirm(f"{mode}-forget this fact?", default=False):
                raise typer.Exit(code=1)
        res = _run(ctx, c.forget, fact_id=fid, mode=mode)
        _emit_forgotten(ctx, res)
        return
    if not query:
        raise typer.BadParameter("need QUERY or --id ID")
    if not match and not hard:
        try:
            plan = c.resolve(query)
        except ApiError as e:  # resolver/LLM unreachable → fall back to literal matching
            if e.status != 503:
                R.error(str(e))
                raise typer.Exit(code=e.exit_code)
            plan = None
            R.warn(f"resolver unavailable ({e}); falling back to literal --match mode")
        if plan is not None:
            _resolve_flow(ctx, query, expect="forget", yes=yes, plan=plan)
            return
    matches = _run(ctx, c.list_facts, q=query, status="any", limit=limit)
    matches = [m for m in matches if m.get("status") not in ("deleted",)]
    if not matches:
        R.warn(f"no facts match '{query}'")
        raise typer.Exit(code=1)
    if not _ctx(ctx).json:
        R.console.print(R.facts_table(matches, title=f"matches for '{query}'"))
    if not yes:
        if not typer.confirm(f"{mode}-forget ALL {len(matches)} fact(s) above?", default=False):
            raise typer.Exit(code=1)
    forgotten: list = []
    for m in matches:
        res = _run(ctx, c.forget, fact_id=str(m["id"]), mode=mode)
        forgotten.extend(res.get("forgotten") or [m["id"]])
    _emit_forgotten(ctx, {"forgotten": forgotten, "mode": mode})


def _emit_forgotten(ctx: typer.Context, res: dict) -> None:
    if _ctx(ctx).json:
        R.print_json(res)
        return
    items = res.get("forgotten") or []
    if not items:
        R.warn("nothing forgotten")
        return
    ids = [R.short_id(i.get("id") if isinstance(i, dict) else i) for i in items]
    R.ok(f"forgot {len(ids)} fact(s): " + ", ".join(ids))


# ======================================================= Facts & time travel
@app.command(rich_help_panel=P_FACTS)
def facts(
    ctx: typer.Context,
    subject: Optional[str] = typer.Option(None, "--subject", "-S", help="Filter by subject."),
    predicate: Optional[str] = typer.Option(None, "--predicate", "-P", help="Filter by predicate."),
    status: str = typer.Option("active", "--status",
                               help="active | staging | superseded | retracted | any"),
    layer: Optional[str] = typer.Option(None, "--layer", "-L",
                                        help="semantic | profile | procedural"),
    q: Optional[str] = typer.Option(None, "-q", "--query", help="Text search over the triple."),
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows."),
    offset: int = typer.Option(0, "--offset", help="Pagination offset."),
):
    """List facts as a table: id (short) · subject · predicate · value · layer · conf · trust ·
    source · asserted · status.

    [dim]Examples:[/dim]
      [cyan]astoria facts[/cyan]                                (active facts)
      [cyan]astoria facts -q beer --status any[/cyan]
      [cyan]astoria facts --subject alice --layer profile[/cyan]
      [cyan]astoria facts --predicate uses_tool --status superseded[/cyan]
      [cyan]astoria --json facts --limit 500 | jq '.[].value'[/cyan]
    """
    c = _ctx(ctx).client
    rows = _run(ctx, c.list_facts, subject=subject, predicate=predicate, status=status,
                layer=layer, q=q, limit=limit, offset=offset)
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty("facts")
        return
    R.console.print(R.facts_table(rows, title=f"facts [{status}]"))


@app.command(rich_help_panel=P_FACTS)
def fact(
    ctx: typer.Context,
    fact_id: str = typer.Argument(..., help="Fact id (full UUID or unique short prefix)."),
    chain: bool = typer.Option(True, "--chain/--no-chain",
                               help="Also show the supersede chain for the key."),
):
    """Full detail for one fact incl. provenance: source / source_kind / origin episode /
    evidence / valid window / supersede links.

    [dim]Example:[/dim]  [cyan]astoria fact 3f2a9c1e[/cyan]
    """
    c = _ctx(ctx).client
    fid = _fact_id(ctx, fact_id)
    f = _run(ctx, c.get_fact, fid)
    if _ctx(ctx).json:
        R.print_json(f)
        return
    R.console.print(R.fact_detail(f))
    if chain and f.get("subject") and f.get("predicate"):
        try:
            hist = c.history(f["subject"], f["predicate"])
        except ApiError:
            hist = []
        if len(hist) > 1:
            R.console.print(R.history_timeline(hist, f["subject"], f["predicate"]))


@app.command(rich_help_panel=P_FACTS)
def history(
    ctx: typer.Context,
    subject: str = typer.Argument(..., help="Subject."),
    predicate: str = typer.Argument(..., help="Predicate."),
):
    """The supersede chain for (subject, predicate) as a timeline, oldest → newest.

    [dim]Example:[/dim]  [cyan]astoria history alice favorite_beer[/cyan]
    """
    c = _ctx(ctx).client
    rows = _run(ctx, c.history, subject, predicate)
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty(f"history for {subject} {predicate}")
        return
    R.console.print(R.history_timeline(rows, subject, predicate))


@app.command("as-of", rich_help_panel=P_FACTS)
def as_of(
    ctx: typer.Context,
    date: str = typer.Argument(..., help="Point in real-world time (YYYY-MM-DD / ISO / "
                                         "'6 months ago')."),
    subject: Optional[str] = typer.Option(None, "--subject", "-S", help="Filter by subject."),
    predicate: Optional[str] = typer.Option(None, "--predicate", "-P", help="Filter by predicate."),
    believed_at: Optional[str] = typer.Option(
        None, "--believed-at", help="Bitemporal: only what the system had been told by this time."),
    q: Optional[str] = typer.Option(None, "-q", "--query", help="Optional text filter."),
):
    """Time travel: facts that were valid at DATE (valid_from ≤ DATE < valid_to), optionally
    as the system believed them at [cyan]--believed-at[/cyan].

    [dim]Examples:[/dim]
      [cyan]astoria as-of 2025-01-01[/cyan]
      [cyan]astoria as-of "1 year ago" --subject alice --predicate lives_in[/cyan]
      [cyan]astoria as-of 2025-06-01 --believed-at 2025-06-01[/cyan]
    """
    c = _ctx(ctx).client
    at = parse_date(date)
    rows = _run(ctx, c.as_of, at, as_believed_at=parse_date(believed_at), subject=subject,
                predicate=predicate, query=q)
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty(f"facts valid at {R.fmt_dt(at)}")
        return
    R.console.print(R.facts_table(rows, title=f"facts valid at {R.fmt_dt(at)}"))


@app.command(rich_help_panel=P_FACTS)
def staging(
    ctx: typer.Context,
    limit: int = typer.Option(100, "--limit", "-n", help="Max rows."),
):
    """List [yellow]staging[/yellow] facts (low-confidence extractions, not recalled) with
    approve hints.

    [dim]Example:[/dim]  [cyan]astoria staging[/cyan]  then  [cyan]astoria approve 3f2a9c1e[/cyan]
    """
    c = _ctx(ctx).client
    rows = _run(ctx, c.list_facts, status="staging", limit=limit)
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty("staging facts — nothing to review")
        return
    R.console.print(R.facts_table(rows, title="staging"))
    R.console.print("[dim]approve with:[/dim]")
    for r in rows[:10]:
        R.console.print(f"  [cyan]astoria approve {R.short_id(r.get('id'))}[/cyan]   "
                        f"[dim]# {escape(str(r.get('subject')))} {escape(str(r.get('predicate')))} "
                        f"{escape(R.trunc(r.get('value'), 40))}[/dim]")
    if len(rows) > 10:
        R.console.print(f"  [dim]… and {len(rows) - 10} more[/dim]")
    R.console.print("[dim]reject with:[/dim]  [cyan]astoria forget --id ID[/cyan]")


@app.command(rich_help_panel=P_FACTS)
def approve(
    ctx: typer.Context,
    fact_ids: list[str] = typer.Argument(..., help="One or more fact ids (short ok)."),
):
    """Promote staging fact(s) to active.

    [dim]Example:[/dim]  [cyan]astoria approve 3f2a9c1e 9b1d[/cyan]
    """
    c = _ctx(ctx).client
    out = []
    for ident in fact_ids:
        fid = _fact_id(ctx, ident)
        res = _run(ctx, c.approve, fid)
        out.append(res)
        if not _ctx(ctx).json:
            f = res.get("fact") or res
            R.ok("approved " + _fact_line(f))
    if _ctx(ctx).json:
        R.print_json(out if len(out) > 1 else out[0])


# ======================================================== Episodes & capture
def _print_capture(res: dict) -> None:
    if res.get("dropped"):
        R.warn(f"dropped by the capture gate: {res.get('dropped')}")
        return
    eid = R.short_id(res.get("episode_id"))
    bits = [f"episode {eid}"]
    if res.get("deduped"):
        bits.append("[yellow]deduped[/yellow] (already stored)")
    bits.append("queued for cognify" if res.get("queued") else "not queued")
    R.ok(" · ".join(bits))
    det = res.get("detector")
    if det:
        R.console.print(f"  [dim]detector:[/dim] {det.get('op')} "
                        f"[bold]{escape(str(det.get('subject')))}[/bold] "
                        f"[cyan]{escape(str(det.get('predicate')))}[/cyan] "
                        f"{escape(str(det.get('value')))}")


@app.command(rich_help_panel=P_EPI)
def capture(
    ctx: typer.Context,
    text: Optional[str] = typer.Option(None, "--text", help="Free text (note / summary)."),
    user_input: Optional[str] = typer.Option(None, "--user-input", help="User side of a turn."),
    agent_response: Optional[str] = typer.Option(None, "--agent-response",
                                                 help="Agent side of a turn."),
    kind: str = typer.Option("note", "--kind", help="turn | summary | note (turn when both "
                                                      "sides are given)."),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Session id."),
    occurred_at: Optional[str] = typer.Option(None, "--at", help="When it happened (default now)."),
    importance: Optional[float] = typer.Option(None, "--importance", help="0..1 (default .5)."),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags."),
    no_cognify: bool = typer.Option(False, "--no-cognify",
                                    help="Store the episode but don't queue fact extraction."),
    priority: str = typer.Option("normal", "--priority", help="normal | high (jump the queue)."),
    stdin: bool = typer.Option(False, "--stdin", help="Read --text from standard input."),
):
    """Capture an episode: a conversation turn (user + agent), a summary, or a note. Episodes
    are stored first and durably; the cognify worker extracts facts afterwards.

    [dim]Examples:[/dim]
      [cyan]astoria capture --text "Decided to pin vLLM 0.20.2 on the RDNA4 box"[/cyan]
      [cyan]astoria capture --user-input "I moved to Portland" --agent-response "Noted!" --session s1[/cyan]
      [cyan]astoria capture --kind summary --text "..." --session s1 --priority high[/cyan]
      [cyan]cat notes.txt | astoria capture --stdin --no-cognify[/cyan]
    """
    if stdin:
        text = sys.stdin.read()
    if user_input and agent_response:
        if kind == "note":
            kind = "turn"
    elif not text:
        raise typer.BadParameter("need --text (or --stdin), or --user-input + --agent-response")
    c = _ctx(ctx).client
    res = _run(ctx, c.capture, kind=kind, text=text, user_input=user_input,
               agent_response=agent_response, session_id=session,
               occurred_at=parse_date(occurred_at), importance=importance, tags=_split_csv(tags),
               cognify=not no_cognify, priority=priority)
    if _ctx(ctx).json:
        R.print_json(res)
    else:
        _print_capture(res)


@app.command(rich_help_panel=P_EPI)
def episodes(
    ctx: typer.Context,
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Filter by session id."),
    kind: Optional[str] = typer.Option(None, "--kind", help="turn | summary | note | import"),
    limit: int = typer.Option(30, "--limit", "-n", help="Max rows (newest first)."),
):
    """List episodes (working memory turns, summaries, notes, imports).

    [dim]Examples:[/dim]  [cyan]astoria episodes --kind summary[/cyan]  ·  [cyan]astoria episodes --session s1 -n 50[/cyan]
    """
    c = _ctx(ctx).client
    rows = _run(ctx, c.list_episodes, session_id=session, kind=kind, limit=limit)
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty("episodes")
        return
    R.console.print(R.episodes_table(rows))


@episode_app.command("delete")
def episode_delete(
    ctx: typer.Context,
    episode_id: str = typer.Argument(..., help="Episode id (full UUID)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
):
    """Delete one episode (facts already extracted from it are kept).

    [dim]Example:[/dim]  [cyan]astoria episode delete 7c0e...[/cyan]
    """
    if not yes and not _ctx(ctx).json:
        if not typer.confirm(f"delete episode {episode_id}?", default=False):
            raise typer.Exit(code=1)
    c = _ctx(ctx).client
    res = _run(ctx, c.delete_episode, episode_id)
    if _ctx(ctx).json:
        R.print_json(res)
    else:
        R.ok(f"deleted episode {R.short_id(episode_id)}")


# ====================================================================== Admin
@app.command(rich_help_panel=P_ADMIN)
def predicates(
    ctx: typer.Context,
    auto: bool = typer.Option(False, "--auto",
                              help="Only auto-registered predicates (created by the extractor — "
                                   "review their cardinality)."),
):
    """List the predicate registry: cardinality (functional = one current value, set = many)
    and layer hint.

    [dim]Examples:[/dim]  [cyan]astoria predicates[/cyan]  ·  [cyan]astoria predicates --auto[/cyan]
    """
    c = _ctx(ctx).client
    rows = _run(ctx, c.predicates)
    if isinstance(rows, dict):  # tolerate {predicates:[...]} shape
        rows = rows.get("predicates") or rows.get("items") or []
    if auto:
        rows = [r for r in rows if r.get("auto")]
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty("predicates")
        return
    R.console.print(R.predicates_table(rows))


@predicate_app.command("set")
def predicate_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Predicate name (snake_case)."),
    functional: Optional[bool] = typer.Option(None, "--functional/--set",
                                              help="Cardinality: one current value / many values."),
    layer: Optional[str] = typer.Option(None, "--layer",
                                        help="Layer hint: semantic | profile | procedural."),
):
    """Set a predicate's cardinality and/or layer hint (PATCH /predicates/NAME).

    [dim]Examples:[/dim]
      [cyan]astoria predicate set favorite_beer --functional[/cyan]
      [cyan]astoria predicate set likes --set --layer profile[/cyan]
    """
    if functional is None and not layer:
        raise typer.BadParameter("give --functional / --set and/or --layer")
    card = None if functional is None else ("functional" if functional else "set")
    c = _ctx(ctx).client
    res = _run(ctx, c.set_predicate, name, cardinality=card, layer_hint=layer)
    if _ctx(ctx).json:
        R.print_json(res)
    else:
        r = res.get("predicate", res) if isinstance(res, dict) else {}
        R.ok(f"predicate [bold]{name}[/bold]: cardinality={r.get('cardinality', card)} "
             f"layer_hint={r.get('layer_hint', layer)}")


@app.command(rich_help_panel=P_ADMIN)
def audit(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows (newest first)."),
):
    """Audit log for the user — who asserted / retracted / forgot / approved what, when.

    [dim]Example:[/dim]  [cyan]astoria audit -n 100[/cyan]
    """
    c = _ctx(ctx).client
    rows = _run(ctx, c.audit, limit=limit)
    if isinstance(rows, dict):
        rows = rows.get("audit") or rows.get("items") or rows.get("rows") or []
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty("audit entries")
        return
    R.console.print(R.generic_table(rows, "audit", prefer=(
        "at", "created_at", "ts", "action", "actor", "source", "fact_id", "subject", "predicate",
        "value", "reason", "detail")))


@app.command(rich_help_panel=P_ADMIN)
def queue(ctx: typer.Context):
    """Cognify queue stats (pending / dead / in-flight). Uses POST /op queue_stats and falls
    back to the queue block of /health.

    [dim]Example:[/dim]  [cyan]astoria queue[/cyan]
    """
    c = _ctx(ctx).client
    source = "op:queue_stats"
    try:
        stats = c.op("queue_stats")
        if isinstance(stats, dict) and "error" in stats:
            raise ApiError(str(stats["error"]))
    except ApiError:
        source = "health.queue"
        h = _run(ctx, c.health)
        stats = (h or {}).get("queue") or {}
    if isinstance(stats, dict) and isinstance(stats.get("queue"), dict):
        stats = stats["queue"]
    if _ctx(ctx).json:
        R.print_json(stats)
        return
    if not isinstance(stats, dict) or not stats:
        R.empty("queue stats")
        return
    R.console.print(R.queue_table(stats, source))


@app.command("wipe-user", rich_help_panel=P_ADMIN)
def wipe_user(
    ctx: typer.Context,
    user_id: str = typer.Argument(..., help="The user to erase — ALL facts, episodes, profile."),
    yes: bool = typer.Option(False, "--yes", help="Required. Acknowledge this is destructive."),
    force: bool = typer.Option(False, "--force", help="Skip the interactive typed confirmation "
                                                        "(for scripts/tests)."),
):
    """[bold red]DANGEROUS[/bold red]: erase everything Astoria knows about USER_ID
    (DELETE /users/{user}). Requires [cyan]--yes[/cyan] and a typed confirmation.

    [dim]Example:[/dim]  [cyan]astoria wipe-user test-user --yes[/cyan]
    """
    if not yes:
        R.error("refusing: pass --yes to acknowledge this deletes ALL memory for the user")
        raise typer.Exit(code=2)
    if not force:
        typed = typer.prompt(f"type the user id '{user_id}' to confirm")
        if typed.strip() != user_id:
            R.error("user id mismatch — aborted")
            raise typer.Exit(code=1)
        if not typer.confirm(f"really wipe '{user_id}' on {_ctx(ctx).client.base_url}?",
                             default=False):
            raise typer.Exit(code=1)
    c = _ctx(ctx).client
    res = _run(ctx, c.wipe_user, user_id)
    if _ctx(ctx).json:
        R.print_json(res)
    else:
        R.ok(f"wiped user [bold]{user_id}[/bold]" + (f": {R._kv(res)}" if isinstance(res, dict)
                                                       and res else ""))


# ============================================================ Graph & aliases
def _graph_node_arg(node: str) -> str:
    """Bare short fact ids are not resolvable here (entity names may look like anything) — pass through."""
    return node.strip()


@app.command(rich_help_panel=P_GRAPH)
def graph(
    ctx: typer.Context,
    node: str = typer.Argument(..., help="Entity name (e.g. buildbot), 'entity:NAME' or 'fact:UUID'."),
    depth: int = typer.Option(2, "--depth", "-d", min=0, max=6, help="Hops to walk (undirected)."),
    fanout: Optional[int] = typer.Option(None, "--fanout", help="Max edges followed per node per hop."),
):
    """Walk the memory graph around NODE: a tree of reachable entities/facts with the relation of
    each hop (GET /graph). Aliases resolve to their canonical entity.

    [dim]Examples:[/dim]  [cyan]astoria graph buildbot[/cyan]  ·  [cyan]astoria graph workstation-1 --depth 1[/cyan]
    """
    c = _ctx(ctx).client
    g = _run(ctx, c.graph, _graph_node_arg(node), depth=depth, fanout=fanout)
    if _ctx(ctx).json:
        R.print_json(g)
        return
    if not (g.get("nodes") or []):
        R.empty("graph nodes")
        return
    R.console.print(R.graph_tree(g))


@app.command(rich_help_panel=P_GRAPH)
def edges(
    ctx: typer.Context,
    node: Optional[str] = typer.Option(None, "--node", "-N", help="Only edges touching this node "
                                                                 "(entity name / entity:NAME / fact:UUID)."),
    relation: Optional[str] = typer.Option(None, "--relation", "-r", help="Only this relation (snake_case)."),
    depth: int = typer.Option(0, "--depth", "-d", min=0, max=6,
                              help="With --node: also edges within DEPTH hops."),
    status: str = typer.Option("active", "--status", help="active | retracted | archived | superseded | any"),
    limit: int = typer.Option(200, "--limit", "-n"),
):
    """List graph edges (GET /edges): src —relation→ dst with weight, confidence, provenance.

    [dim]Examples:[/dim]  [cyan]astoria edges[/cyan]  ·  [cyan]astoria edges --node buildbot --depth 1[/cyan]
    ·  [cyan]astoria edges --relation runs_on[/cyan]
    """
    c = _ctx(ctx).client
    rows = _run(ctx, c.list_edges, node=node, relation=relation, depth=depth, status=status, limit=limit)
    if isinstance(rows, dict):
        rows = rows.get("edges") or rows.get("items") or []
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty("edges")
        return
    R.console.print(R.edges_table(rows))


@edge_app.command("add")
def edge_add(
    ctx: typer.Context,
    src: str = typer.Argument(..., help="Source node: entity name, entity:NAME or fact:UUID."),
    relation: str = typer.Argument(..., help="snake_case relation: part_of, located_in, works_at, owns, "
                                             "runs_on, depends_on, related_to ..."),
    dst: str = typer.Argument(..., help="Destination node."),
    weight: Optional[float] = typer.Option(None, "--weight", "-w", help="Edge weight (default 1)."),
    confidence: Optional[float] = typer.Option(None, "--confidence", help="0-1 (default .90 explicit)."),
    evidence: Optional[str] = typer.Option(None, "--evidence", help="Verbatim support snippet."),
    valid_from: Optional[str] = typer.Option(None, "--from", help="Valid-from date."),
    valid_to: Optional[str] = typer.Option(None, "--to", help="Valid-to date."),
):
    """Assert an edge SRC —RELATION→ DST (POST /edges). Idempotent: re-adding an active edge bumps it.
    Entity endpoints are auto-registered; aliases resolve to their canonical entity.

    [dim]Example:[/dim]  [cyan]astoria edge add buildbot runs_on workstation-1[/cyan]
    """
    c = _ctx(ctx).client
    res = _run(ctx, c.add_edge, src, relation, dst, weight=weight, confidence=confidence, evidence=evidence,
               valid_from=parse_date(valid_from), valid_to=parse_date(valid_to))
    if _ctx(ctx).json:
        R.print_json(res)
        return
    e = res.get("edge") or {}
    R.ok(f"{res.get('action', 'ok')}: [bold]{escape(str(e.get('src')))}[/bold] "
         f"[magenta]{escape(str(e.get('relation')))}[/magenta] [bold]{escape(str(e.get('dst')))}[/bold] "
         f"[dim](id {R.short_id(e.get('id'))} · conf {R.fmt_num(e.get('confidence'))})[/dim]")


@edge_app.command("rm")
def edge_rm(
    ctx: typer.Context,
    edge_id: str = typer.Argument(..., help="Edge id (full uuid or unique prefix as printed by `edges`)."),
    hard: bool = typer.Option(False, "--hard", help="Delete the row instead of retracting it."),
):
    """Retract (default) or hard-delete an edge (DELETE /edges/ID).

    [dim]Example:[/dim]  [cyan]astoria edge rm 3f2a1c9b[/cyan]
    """
    c = _ctx(ctx).client
    eid = edge_id.strip()
    if len(eid) < 32:
        rows = _run(ctx, c.list_edges, status="any", limit=2000)
        matches = sorted({str(r.get("id")) for r in rows if str(r.get("id", "")).startswith(eid)})
        if len(matches) != 1:
            R.error(f"{'no' if not matches else 'ambiguous'} edge id starting with '{eid}'")
            raise typer.Exit(code=EXIT_ERROR)
        eid = matches[0]
    res = _run(ctx, c.delete_edge, eid, mode="hard" if hard else "retract")
    if _ctx(ctx).json:
        R.print_json(res)
    else:
        e = res.get("edge") or {}
        R.ok(f"{res.get('mode')}: {escape(str(e.get('src')))} {escape(str(e.get('relation')))} "
             f"{escape(str(e.get('dst')))} [dim]({R.short_id(eid)})[/dim]")


@alias_app.command("add")
def alias_add(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="The other name."),
    canonical: str = typer.Argument(..., help="The name to keep (what fact.subject holds)."),
):
    """Declare ALIAS to mean CANONICAL (POST /aliases): every later write/read on ALIAS lands on
    CANONICAL. Chains are flattened; the user_id itself cannot be aliased away.

    [dim]Example:[/dim]  [cyan]astoria alias add ws1 workstation-1[/cyan]
    """
    c = _ctx(ctx).client
    res = _run(ctx, c.add_alias, alias, canonical)
    if _ctx(ctx).json:
        R.print_json(res)
    else:
        a = res.get("alias") or {}
        extra = f" · re-pointed {res['repointed']}" if res.get("repointed") else ""
        R.ok(f"{res.get('action', 'ok')}: [bold]{escape(str(a.get('alias')))}[/bold] → "
             f"[cyan]{escape(str(a.get('canonical')))}[/cyan]{extra}")


@alias_app.command("list")
def alias_list(
    ctx: typer.Context,
    canonical: Optional[str] = typer.Option(None, "--canonical", "-c", help="Only aliases of this canonical name."),
):
    """List subject aliases (GET /aliases).

    [dim]Example:[/dim]  [cyan]astoria alias list[/cyan]  ·  [cyan]astoria alias list -c workstation-1[/cyan]
    """
    c = _ctx(ctx).client
    rows = _run(ctx, c.list_aliases, canonical=canonical)
    if isinstance(rows, dict):
        rows = rows.get("aliases") or rows.get("items") or []
    if _ctx(ctx).json:
        R.print_json(rows)
        return
    if not rows:
        R.empty("aliases")
        return
    R.console.print(R.aliases_table(rows))


@alias_app.command("rm")
def alias_rm(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="The alias to remove."),
):
    """Remove an alias (DELETE /aliases/ALIAS). Facts already written under the canonical name stay.

    [dim]Example:[/dim]  [cyan]astoria alias rm ws1[/cyan]
    """
    c = _ctx(ctx).client
    res = _run(ctx, c.delete_alias, alias)
    if _ctx(ctx).json:
        R.print_json(res)
    else:
        a = res.get("alias") or {}
        R.ok(f"removed alias [bold]{escape(str(a.get('alias')))}[/bold] → {escape(str(a.get('canonical')))}")


# ======================================================================= Data
@app.command(rich_help_panel=P_DATA)
def export(
    ctx: typer.Context,
    out: Optional[Path] = typer.Option(None, "-o", "--out", help="Write to this file "
                                                                   "(default stdout)."),
    status: str = typer.Option("any", "--status",
                               help="Which facts: any (default, keeps history) | active | …"),
    no_episodes: bool = typer.Option(False, "--no-episodes", help="Facts only."),
    page: int = typer.Option(500, "--page", help="Page size for /facts pagination."),
    episode_limit: int = typer.Option(10000, "--episode-limit", help="Max episodes to pull."),
):
    """Dump facts + episodes for the user to JSON (via the list endpoints, paginated).
    Pair with [cyan]import[/cyan] to move memory between instances.

    [dim]Examples:[/dim]
      [cyan]astoria export -o alice-$(date +%F).json[/cyan]
      [cyan]astoria --user bob export --status active --no-episodes > bob-facts.json[/cyan]
    """
    c = _ctx(ctx).client
    facts_all: list[dict] = []
    offset = 0
    while True:
        batch = _run(ctx, c.list_facts, status=status, limit=page, offset=offset)
        facts_all.extend(batch)
        if len(batch) < page:
            break
        offset += page
    eps: list[dict] = []
    if not no_episodes:
        eps = _run(ctx, c.list_episodes, limit=episode_limit)
    payload = {"astoria_export": 1, "user_id": c.user, "source_url": c.base_url,
               "exported_at": datetime.now(timezone.utc).isoformat(),
               "facts": facts_all, "episodes": eps}
    text = json.dumps(payload, indent=2, default=str)
    if out:
        out.write_text(text)
        R.ok(f"exported {len(facts_all)} fact(s), {len(eps)} episode(s) → {out}")
    else:
        print(text)


@app.command("import", rich_help_panel=P_DATA)
def import_(
    ctx: typer.Context,
    file: Path = typer.Argument(..., exists=True, readable=True, dir_okay=False,
                                help="JSON file written by [cyan]astoria export[/cyan]."),
    all_statuses: bool = typer.Option(False, "--all",
                                      help="Also replay superseded/retracted facts as historical "
                                           "(default: active only)."),
    episodes: bool = typer.Option(False, "--episodes",
                                  help="Also replay summary/note episodes via /capture "
                                       "(no cognify)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be sent."),
):
    """Replay an export into the target user via POST /facts (explicit; valid window,
    layer, cardinality, tags, confidence preserved where the API allows). Simple, idempotent
    enough to re-run: identical triples are no-ops server-side.

    [dim]Examples:[/dim]
      [cyan]astoria import alice-2026-08-22.json[/cyan]
      [cyan]astoria --user alice-test import alice.json --all --episodes --dry-run[/cyan]
    """
    c = _ctx(ctx).client
    data = json.loads(file.read_text())
    facts_in = data.get("facts") or []
    eps_in = data.get("episodes") or []
    sent = skipped = failed = 0
    results: list[dict] = []
    ordered = sorted(facts_in, key=lambda f: str(f.get("asserted_at") or ""))
    for f in ordered:
        st = f.get("status")
        if st == "active":
            historical = False
        elif all_statuses and st in ("superseded", "retracted"):
            historical = True
        else:
            skipped += 1
            continue
        body = {k: f.get(k) for k in ("valid_from", "valid_to", "confidence", "layer", "tags",
                                      "cardinality") if f.get(k) is not None}
        if historical:
            body["historical"] = True
        if dry_run:
            R.console.print(f"[dim]POST /facts[/dim] {escape(str(f.get('subject')))} "
                            f"{escape(str(f.get('predicate')))} "
                            f"{escape(R.trunc(f.get('value'), 50))} {R._kv(body)}")
            continue
        try:
            res = c.add_fact(f["subject"], f["predicate"], f["value"], **body)
            results.append(res)
            sent += 1
        except ApiError as e:
            failed += 1
            R.warn(escape(f"{f.get('subject')} {f.get('predicate')} "
                          f"{R.trunc(f.get('value'), 40)}: {e}"))
    ep_sent = 0
    if episodes:
        for e in eps_in:
            kind = e.get("kind")
            if kind not in ("summary", "note", "import"):
                continue
            text = e.get("body") or e.get("text") or e.get("hook")
            if not text:
                continue
            body = {"kind": "note" if kind == "import" else kind, "text": text,
                    "session_id": e.get("session_id"), "occurred_at": e.get("occurred_at"),
                    "importance": e.get("importance"), "tags": e.get("tags"),
                    "cognify": False}
            if dry_run:
                R.console.print(f"[dim]POST /capture[/dim] {kind} {escape(R.trunc(text, 60))}")
                continue
            try:
                c.capture(**body)
                ep_sent += 1
            except ApiError as ex:
                failed += 1
                R.warn(f"episode {R.short_id(e.get('id'))}: {ex}")
    summary = {"facts_sent": sent, "facts_skipped": skipped, "episodes_sent": ep_sent,
               "failed": failed, "dry_run": dry_run}
    if _ctx(ctx).json:
        R.print_json({**summary, "results": results})
    else:
        R.ok(f"import: {sent} fact(s) sent, {skipped} skipped, {ep_sent} episode(s), "
             f"{failed} failed" + (" [dry run]" if dry_run else ""))
    if failed:
        raise typer.Exit(code=EXIT_ERROR)


if __name__ == "__main__":  # python -m astoria.cli.main
    app()
