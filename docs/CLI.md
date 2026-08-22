# Astoria — CLI (`astoria`)

`astoria` is a thin client over the REST API (it never touches the database). It is installed with the
package (`pip install .` → console script `astoria`) and talks to any Astoria service reachable over
HTTP. This page covers setup, the workflows the CLI is designed around, and the complete `--help` of
every command.

## 1. Setup

```bash
pip install .                                     # or: pipx install .
export ASTORIA_URL=http://nas.local:8933          # service base URL (default http://localhost:8933)
export ASTORIA_USER=alice                         # optional: user_id for every call (default: the server's ASTORIA_USER_DEFAULT)
export ASTORIA_TOKEN=<token>                      # optional: bearer token → client name + trust cap server-side
astoria status                                    # db / embeddings / llm / rerank / queue at a glance
```

| env | CLI flag | meaning |
|---|---|---|
| `ASTORIA_URL` | `--url` | service base URL |
| `ASTORIA_USER` | `--user / -u` | `user_id` every request is scoped to; empty → the server applies `ASTORIA_USER_DEFAULT` |
| `ASTORIA_TOKEN` | `--token` | `Authorization: Bearer …`; without it the CLI sends `X-Astoria-Client: cli` |
| — | `--json / -j` | raw JSON instead of tables (for `jq`) |
| — | `--timeout` | HTTP timeout (30 s) |

Exit codes: `0` ok · `1` generic error · `3` could not reach the service · `4` the service rejected the
request (4xx) · `5` the service failed (5xx). Short fact ids (the first 8 characters shown in tables) are
accepted wherever an id is expected; dates accept `YYYY-MM-DD`, ISO-8601, or `now` / `today` /
`yesterday` / `3 days ago` / `2 weeks ago`.

> The help text below was generated with `COLUMNS=100` from the installed command.

## 2. Workflows

### Ask memory

```bash
astoria recall "what editor do I use"             # context block + ranked table (score · conf · layer · text · stale?)
astoria recall "family" --layers profile,semantic --limit 5 --json | jq .context
astoria recall "where did I live" --as-of 2024-01-01          # valid-axis time travel (BM25-ranked)
astoria briefing                                  # stable prompt prefix: narrative + profile + top semantic facts
astoria profile                                   # who the user is (narrative + profile-layer facts)
```

### State, correct, retract, forget

```bash
astoria remember alice favorite_beer IPA          # explicit fact (conf .90); functional → supersedes, set → adds
astoria remember alice uses_tool Neovim --set     # force set cardinality
astoria remember alice employer Acme --from 2019-01-01 --to 2023-12-31 --historical   # past value, current untouched
astoria correct  alice favorite_beer Stout        # same as remember; prints what it replaced
astoria retract  alice uses_tool Emacs            # "no longer true": belief closed, history kept, tombstoned
astoria remember --text "Prefers dark mode and tabs"   # free text → note episode → cognify extracts facts
```

### Natural-language instructions (LLM target resolver)

```bash
astoria resolve "forget the thing about Guinness"    # preview only: intent, targets, new_fact, confidence
astoria forget  "the thing about Guinness"           # resolve → show targets → confirm → soft-forget
astoria forget  "old address" --match                # no LLM: substring match on hooks → confirm
astoria forget  --id 3f2a9c1e --hard --yes           # one fact, row removed, no prompt
```

`forget` falls back to `--match` mode automatically when the resolver returns 503 (LLM unavailable).

### History, time travel, belief axis

```bash
astoria history alice favorite_beer                  # the supersede chain as a timeline (newest first)
astoria as-of 2026-07-01 --predicate favorite_beer   # what was true then (current belief)
astoria as-of today --believed-at 2026-08-19 --predicate favorite_beer   # what the store believed on 08-19
```

### Browse and inspect

```bash
astoria facts -q beer                                # hook substring search
astoria facts --subject alice --layer profile --status any
astoria fact 3f2a9c1e                                # one fact with provenance (source, kind, trust, evidence, lineage)
astoria episodes --session s-42
```

### Review what the extractor produced

```bash
astoria staging                                      # low-confidence / trust-guard-staged extractions
astoria approve 3f2a9c1e 7b0d11aa                    # promote (re-asserted as explicit through the supersede path)
astoria audit --limit 30                             # the mutation log
astoria queue                                        # cognify queue: by state, oldest, dead jobs, embed backlog
```

### Graph and aliases

```bash
astoria graph buildbot                               # neighbourhood of an entity (nodes + edges)
astoria graph buildbot --depth 1
astoria edges --node buildbot --depth 1
astoria edge add buildbot runs_on workstation-1      # assert a typed link
astoria edge rm <edge-id>
astoria alias add ws1 workstation-1                  # two names, one subject; every later write/read on ws1 lands on workstation-1
astoria alias list -c workstation-1
astoria alias rm ws1
```

### Capture turns from a script

```bash
astoria capture --user-input "I moved to Portland" --agent-response "Noted!" --session s-42
echo "Long note text…" | astoria capture --kind note --stdin
```

### Move memory between instances

```bash
astoria export -o alice-$(date +%F).json             # facts (any status) + episodes, paginated
astoria --user alice-test import alice.json --dry-run
astoria --user alice-test import alice.json --all --episodes     # replay history too; summaries/notes via /capture (no cognify)
```

Import replays through `POST /facts` in `asserted_at` order (active rows as live facts; with `--all`,
superseded/retracted rows as `historical`). Identical triples are server-side no-ops, so re-running an
import is safe.

### Admin

```bash
astoria predicates                                   # the registry
astoria predicate set likes --functional             # flip cardinality / layer hint
astoria wipe-user alice-test --yes --force      # DELETE /users/{id} (--force skips the typed confirmation)
astoria status --json | jq .tei.endpoints            # per-endpoint embedder state
```

## 3. Test drive (safe: throwaway user, wiped at the end)

```bash
#!/usr/bin/env bash
set -e
export ASTORIA_URL=${ASTORIA_URL:-http://nas.local:8933}
U=drive-$RANDOM
A="astoria --user $U"
$A remember $U favorite_beer Guinness
$A correct  $U favorite_beer IPA                    # superseded
$A history  $U favorite_beer                        # IPA active, Guinness superseded
$A remember $U likes stout --set
$A remember $U likes IPA --set
$A retract  $U likes stout
$A facts --subject $U --status any
$A as-of "1 hour ago" --predicate favorite_beer
$A recall "what beer do I like"                     # context mentions IPA, never Guinness
$A capture --kind note --text "Actually, my favorite beer is stout" --no-cognify   # detector path, no LLM
$A facts --predicate favorite_beer                  # stout
$A audit --limit 10
$A wipe-user $U --yes --force
```

## 4. Full help (`astoria --help` and every subcommand)

### `astoria --help`

```text

 Usage: astoria [OPTIONS] COMMAND [ARGS]...

 Astoria — layered, trusted, deep memory for agents and humans. This CLI talks to the Astoria
 service over HTTP (it never touches the database).

 Layers
   working     raw turns of one session (per --session) — prepended to recall, never searched
   episodic    summaries / notes / imports — "what happened"
   semantic    (subject, predicate, value) facts — "what is true"
   profile     facts about the user that shape every answer (+ a narrative)
   procedural  how-to knowledge linked to skills / plans / docs

 Trust in two lines
   Every fact carries confidence (explicit .90 · detector .80 · extracted ≤.85 · imported .45) and
 a
   source_trust capped by the client that wrote it; both only rank — a newer human statement always
 wins.
   Low-confidence extractions land in staging (not recalled) until you approve them.

 Common workflows
   astoria recall "what editor do I use"            → context block + ranked items
   astoria remember alice favorite_beer IPA           → assert a fact (supersedes the old value)
   astoria remember --text "Prefers dark mode"       → capture a note; the worker extracts facts
   astoria correct alice favorite_beer Stout          → same as remember, shows what it replaced
   astoria resolve "forget the beer stuff"            → LLM resolves WHICH facts are meant (preview
 only)
   astoria forget "the thing about Guinness"         → resolve → show targets → confirm → apply
   astoria facts -q beer · astoria fact 3f2a        → browse, then inspect provenance
   astoria history alice favorite_beer                → supersede chain as a timeline
   astoria as-of 2026-01-01                          → what was true back then
   astoria staging → astoria approve ID               → review extracted facts
   astoria briefing · astoria profile                  → stable prompt prefix / who the user is
   astoria graph buildbot · astoria edge add A runs_on B   → walk / extend the entity graph
   astoria alias add ws1 workstation-1       → two names, one subject

 Environment
   ASTORIA_URL    service base URL   (default http://localhost:8933)
   ASTORIA_TOKEN  bearer token → client name server-side (sent as Authorization: Bearer)
   ASTORIA_USER   default user_id    (default alice)

 Short fact ids (first 8 chars, as printed in tables) are accepted anywhere an ID is expected.
 Dates accept YYYY-MM-DD, ISO-8601, or "now" / "today" / "yesterday" / "3 days ago" / "2 weeks
 ago".

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --user                -u      <str>    user_id every request is scoped to.                       │
│                                        [env var: ASTORIA_USER]                                   │
│ --url                         <str>    Service base URL.                [env var: ASTORIA_URL]   │
│                                        [default: http://localhost:8933]                          │
│ --token                       <str>    Bearer token (maps to a client name and trust cap         │
│                                        server-side).                                             │
│                                        [env var: ASTORIA_TOKEN]                                  │
│ --json                -j               Print the raw JSON response instead of tables (scripting  │
│                                        / jq).                                                    │
│ --timeout                     <float>  HTTP timeout in seconds. [default: 30.0]                  │
│ --install-completion                   Install completion for the current shell.                 │
│ --show-completion                      Show completion for the current shell, to copy it or      │
│                                        customize the installation.                               │
│ --help                -h               Show this message and exit.                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Read memory ────────────────────────────────────────────────────────────────────────────────────╮
│ status      Service health — db / tei (embeddings) / llm (cognify) / queue — as a table.         │
│ health      Alias of status.                                                                     │
│ recall      Recall memory for a query: prints the ready-to-inject context block and a ranked     │
│             items table (score · conf · layer · text · stale?).                                  │
│ briefing    The stable briefing block — narrative + top profile facts — designed as a cacheable  │
│             prompt prefix.                                                                       │
│ profile     Who the user is: the profile narrative plus every profile-layer fact.                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Write memory ───────────────────────────────────────────────────────────────────────────────────╮
│ remember    Assert a fact explicitly (confidence .90, your client's trust). A functional         │
│             predicate                                                                            │
│             supersedes the previous value; a set predicate adds another value.                   │
│ resolve     Preview how the LLM target-resolver reads a natural-language instruction: which      │
│             stored facts it                                                                      │
│             means and what it would do (forget / retract / correct / remember). Nothing is       │
│             applied unless                                                                       │
│             --apply.                                                                             │
│ correct     Correct a fact — same as remember (POST /correct) but prints what it superseded.     │
│             Given ONE free-text argument instead of a triple, the LLM resolver finds the fact to │
│             replace:                                                                             │
│             preview → confirm → apply.                                                           │
│ retract     Retract a fact: it stops being true now (status → retracted, history kept,           │
│             tombstoned so extraction can't resurrect it). One free-text argument → the LLM       │
│             resolver picks                                                                       │
│             the fact(s): preview → confirm → apply.                                              │
│ forget      Forget facts — stronger than retract: they vanish from history too (soft = hidden,   │
│             --hard = gone). A free-text QUERY goes through the LLM target-resolver: it shows     │
│             WHICH facts it                                                                       │
│             thinks you mean, asks, then soft-forgets them (--match = plain substring search      │
│             instead;                                                                             │
│             --hard implies --match or --id).                                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Facts & time travel ────────────────────────────────────────────────────────────────────────────╮
│ facts       List facts as a table: id (short) · subject · predicate · value · layer · conf ·     │
│             trust ·                                                                              │
│             source · asserted · status.                                                          │
│ fact        Full detail for one fact incl. provenance: source / source_kind / origin episode /   │
│             evidence / valid window / supersede links.                                           │
│ history     The supersede chain for (subject, predicate) as a timeline, oldest → newest.         │
│ as-of       Time travel: facts that were valid at DATE (valid_from ≤ DATE < valid_to),           │
│             optionally                                                                           │
│             as the system believed them at --believed-at.                                        │
│ staging     List staging facts (low-confidence extractions, not recalled) with                   │
│             approve hints.                                                                       │
│ approve     Promote staging fact(s) to active.                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Episodes & capture ─────────────────────────────────────────────────────────────────────────────╮
│ capture     Capture an episode: a conversation turn (user + agent), a summary, or a note.        │
│             Episodes                                                                             │
│             are stored first and durably; the cognify worker extracts facts afterwards.          │
│ episodes    List episodes (working memory turns, summaries, notes, imports).                     │
│ episode     Operate on a single episode (delete). Use astoria episodes to list.                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Admin ──────────────────────────────────────────────────────────────────────────────────────────╮
│ predicates  List the predicate registry: cardinality (functional = one current value, set =      │
│             many)                                                                                │
│             and layer hint.                                                                      │
│ audit       Audit log for the user — who asserted / retracted / forgot / approved what, when.    │
│ queue       Cognify queue stats (pending / dead / in-flight). Uses POST /op queue_stats and      │
│             falls                                                                                │
│             back to the queue block of /health.                                                  │
│ wipe-user   DANGEROUS: erase everything Astoria knows about USER_ID                              │
│             (DELETE /users/{user}). Requires --yes and a typed confirmation.                     │
│ predicate   Operate on a single predicate (set cardinality/layer). Use astoria predicates to     │
│             list.                                                                                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Graph & aliases ────────────────────────────────────────────────────────────────────────────────╮
│ graph       Walk the memory graph around NODE: a tree of reachable entities/facts with the       │
│             relation of                                                                          │
│             each hop (GET /graph). Aliases resolve to their canonical entity.                    │
│ edges       List graph edges (GET /edges): src —relation→ dst with weight, confidence,           │
│             provenance.                                                                          │
│ edge        Operate on a single edge (add / rm). Use astoria edges to list, astoria graph NODE   │
│             to walk.                                                                             │
│ alias       Subject aliases: add ALIAS CANONICAL · list · rm ALIAS. Writes and reads on ALIAS    │
│             land on CANONICAL.                                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Data ───────────────────────────────────────────────────────────────────────────────────────────╮
│ export      Dump facts + episodes for the user to JSON (via the list endpoints, paginated).      │
│             Pair with import to move memory between instances.                                   │
│ import      Replay an export into the target user via POST /facts (explicit; valid window,       │
│             layer, cardinality, tags, confidence preserved where the API allows). Simple,        │
│             idempotent                                                                           │
│             enough to re-run: identical triples are no-ops server-side.                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria status`

```text

 Usage: astoria status [OPTIONS]

 Service health — db / tei (embeddings) / llm (cognify) / queue — as a table.

 Exit code is 0 only when the service reports ok.

 Examples:  astoria status   ·   astoria --json status | jq .queue

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria health`

```text

 Usage: astoria health [OPTIONS]

 Alias of status.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria recall`

```text

 Usage: astoria recall [OPTIONS] {query}

 Recall memory for a query: prints the ready-to-inject context block and a ranked items table
 (score · conf · layer · text · stale?).

 Examples:
 astoria recall "favorite beer"
 astoria recall "deploy steps" --layers procedural,semantic --facts-only
 astoria recall "editor" --as-of 2026-01-01
 astoria recall "what were we doing" --session abc123 --profile
 astoria recall "beer" --context-only >> prompt.txt

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    query      <str>  Natural-language query (hybrid vector + BM25 search). [required]          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --layers        -L      <str>  Comma list of layers to search:                                   │
│                                profile,semantic,procedural,episodic (default: all four).         │
│ --limit         -n      <int>  Max items after collapse. [default: 12]                           │
│ --tokens        -t      <int>  Token budget for the rendered context block (~chars/4).           │
│                                [default: 1000]                                                   │
│ --facts-only                   Skip episodes; facts only.                                        │
│ --as-of                 <str>  Time-travel: facts valid at this date (YYYY-MM-DD / ISO / '2      │
│                                weeks ago').                                                      │
│ --believed-at           <str>  Also restrict to what the system believed at this time.           │
│ --session       -s      <str>  Session id — prepends the last 4 turns as working memory.         │
│ --profile                      Include the profile block (narrative + profile facts).            │
│ --context-only                 Print just the context block (pipe into a prompt).                │
│ --help          -h             Show this message and exit.                                       │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria briefing`

```text

 Usage: astoria briefing [OPTIONS]

 The stable briefing block — narrative + top profile facts — designed as a cacheable prompt prefix.

 Examples:  astoria briefing   ·   astoria briefing --raw > system_prefix.txt

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --tokens  -t      <int>  Token budget. [default: 1200]                                           │
│ --raw                    Print only the context text (no panel).                                 │
│ --help    -h             Show this message and exit.                                             │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria profile`

```text

 Usage: astoria profile [OPTIONS]

 Who the user is: the profile narrative plus every profile-layer fact.

 Example:  astoria --user bob profile

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria remember`

```text

 Usage: astoria remember [OPTIONS] [subject] [predicate] [value]

 Assert a fact explicitly (confidence .90, your client's trust). A functional predicate supersedes
 the previous value; a set predicate adds another value.

 Examples:
 astoria remember alice favorite_beer IPA
 astoria remember alice uses_tool Neovim --set
 astoria remember alice lives_in Portland --from 2024-06-01
 astoria remember alice employer Acme --from 2019-01-01 --to 2023-12-31 --historical
 astoria remember --text "Rick prefers dark mode and tabs over spaces"

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   subject        <str>  Subject ('alice' / 'I' / 'me' → the user).                               │
│   predicate      <str>  snake_case predicate, e.g. favorite_beer.                                │
│   value          <str>  Value text.                                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --text                     <str>    Free-text mode: capture a note episode instead of a triple;  │
│                                     the cognify worker extracts facts from it.                   │
│ --from                     <str>    Valid-from date (real-world time).                           │
│ --to                       <str>    Valid-to date (closes the window).                           │
│ --functional      --set             Cardinality override: functional = one current value         │
│                                     (replaces), set = many values (adds). Default: predicate     │
│                                     registry / heuristic.                                        │
│ --layer                    <str>    semantic | profile | procedural (default: registry).         │
│ --confidence               <float>  Override confidence (explicit default .90).                  │
│ --tags                     <str>    Comma-separated tags.                                        │
│ --historical                        Record as a past value without disturbing the current one.   │
│ --session     -s           <str>    (with --text) session id for the note.                       │
│ --help        -h                    Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria resolve`

```text

 Usage: astoria resolve [OPTIONS] {text}

 Preview how the LLM target-resolver reads a natural-language instruction: which stored facts it
 means and what it would do (forget / retract / correct / remember). Nothing is applied unless
 --apply.

 Examples:
 astoria resolve "forget the beer stuff"
 astoria resolve "actually I moved to Oakland" --apply

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    text      <str>  Natural-language memory instruction. [required]                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --apply            Apply the plan (asks first unless --yes).                                     │
│ --yes    -y        With --apply: skip the confirmation prompt.                                   │
│ --help   -h        Show this message and exit.                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria correct`

```text

 Usage: astoria correct [OPTIONS] {subject} [predicate] [value]

 Correct a fact — same as remember (POST /correct) but prints what it superseded. Given ONE
 free-text argument instead of a triple, the LLM resolver finds the fact to replace: preview →
 confirm → apply.

 Examples:
 astoria correct alice favorite_beer Stout
 astoria correct "actually I live in Oakland"

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    subject        <str>  Subject — or a free-text correction ("actually I live in Oakland")    │
│                            when no PREDICATE follows.                                            │
│                            [required]                                                            │
│      predicate      <str>  Predicate.                                                            │
│      value          <str>  The new, correct value.                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --from          <str>  When the new value became true.                                           │
│ --yes   -y             (free-text mode) skip the confirmation prompt.                            │
│ --help  -h             Show this message and exit.                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria retract`

```text

 Usage: astoria retract [OPTIONS] [subject] [predicate] [value]

 Retract a fact: it stops being true now (status → retracted, history kept, tombstoned so
 extraction can't resurrect it). One free-text argument → the LLM resolver picks the fact(s):
 preview → confirm → apply.

 Examples:
 astoria retract alice favorite_beer         (all values of the key)
 astoria retract alice uses_tool Emacs       (one value)
 astoria retract --id 3f2a9c1e
 astoria retract "I don't use Emacs anymore"

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   subject        <str>  Subject — or a free-text statement ("I don't use Emacs anymore") when no │
│                         PREDICATE follows.                                                       │
│   predicate      <str>  Predicate.                                                               │
│   value          <str>  Value (omit to retract every value of the key).                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --id            <str>  Retract by fact id instead.                                               │
│ --yes   -y             (free-text mode) skip the confirmation prompt.                            │
│ --help  -h             Show this message and exit.                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria forget`

```text

 Usage: astoria forget [OPTIONS] [query]

 Forget facts — stronger than retract: they vanish from history too (soft = hidden, --hard = gone).
 A free-text QUERY goes through the LLM target-resolver: it shows WHICH facts it thinks you mean,
 asks, then soft-forgets them (--match = plain substring search instead; --hard implies --match or
 --id).

 Examples:
 astoria forget --id 3f2a9c1e
 astoria forget "the thing about Guinness"   (resolve → preview → confirm)
 astoria forget "old address" --match        (substring preview → confirm)
 astoria forget "old address" --match --hard --yes

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   query      <str>  Natural-language instruction (LLM-resolved) — or, with --match, literal      │
│                     search text.                                                                 │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --id             <str>  Forget exactly this fact id.                                             │
│ --hard                  Hard delete (row removed). Default is soft (status=deleted, recoverable, │
│                         tombstoned).                                                             │
│ --yes    -y             Skip the confirmation prompt.                                            │
│ --match  -m             Literal mode: substring-match QUERY against fact hooks (no LLM); lists   │
│                         the matches and asks before acting.                                      │
│ --limit          <int>  (--match mode) max matches to show/forget. [default: 25]                 │
│ --help   -h             Show this message and exit.                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria facts`

```text

 Usage: astoria facts [OPTIONS]

 List facts as a table: id (short) · subject · predicate · value · layer · conf · trust · source ·
 asserted · status.

 Examples:
 astoria facts                                (active facts)
 astoria facts -q beer --status any
 astoria facts --subject alice --layer profile
 astoria facts --predicate uses_tool --status superseded
 astoria --json facts --limit 500 | jq '.[].value'

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --subject    -S      <str>  Filter by subject.                                                   │
│ --predicate  -P      <str>  Filter by predicate.                                                 │
│ --status             <str>  active | staging | superseded | retracted | any [default: active]    │
│ --layer      -L      <str>  semantic | profile | procedural                                      │
│ --query      -q      <str>  Text search over the triple.                                         │
│ --limit      -n      <int>  Max rows. [default: 50]                                              │
│ --offset             <int>  Pagination offset. [default: 0]                                      │
│ --help       -h             Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria fact`

```text

 Usage: astoria fact [OPTIONS] {fact_id}

 Full detail for one fact incl. provenance: source / source_kind / origin episode / evidence /
 valid window / supersede links.

 Example:  astoria fact 3f2a9c1e

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    fact_id      <str>  Fact id (full UUID or unique short prefix). [required]                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --chain      --no-chain      Also show the supersede chain for the key. [default: chain]         │
│ --help   -h                  Show this message and exit.                                         │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria history`

```text

 Usage: astoria history [OPTIONS] {subject} {predicate}

 The supersede chain for (subject, predicate) as a timeline, oldest → newest.

 Example:  astoria history alice favorite_beer

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    subject        <str>  Subject. [required]                                                   │
│ *    predicate      <str>  Predicate. [required]                                                 │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria as-of`

```text

 Usage: astoria as-of [OPTIONS] {date}

 Time travel: facts that were valid at DATE (valid_from ≤ DATE < valid_to), optionally as the
 system believed them at --believed-at.

 Examples:
 astoria as-of 2025-01-01
 astoria as-of "1 year ago" --subject alice --predicate lives_in
 astoria as-of 2025-06-01 --believed-at 2025-06-01

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    date      <str>  Point in real-world time (YYYY-MM-DD / ISO / '6 months ago'). [required]   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --subject      -S      <str>  Filter by subject.                                                 │
│ --predicate    -P      <str>  Filter by predicate.                                               │
│ --believed-at          <str>  Bitemporal: only what the system had been told by this time.       │
│ --query        -q      <str>  Optional text filter.                                              │
│ --help         -h             Show this message and exit.                                        │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria staging`

```text

 Usage: astoria staging [OPTIONS]

 List staging facts (low-confidence extractions, not recalled) with approve hints.

 Example:  astoria staging  then  astoria approve 3f2a9c1e

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --limit  -n      <int>  Max rows. [default: 100]                                                 │
│ --help   -h             Show this message and exit.                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria approve`

```text

 Usage: astoria approve [OPTIONS] {fact_ids}...

 Promote staging fact(s) to active.

 Example:  astoria approve 3f2a9c1e 9b1d

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    fact_ids      <str>  One or more fact ids (short ok). [required]                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria capture`

```text

 Usage: astoria capture [OPTIONS]

 Capture an episode: a conversation turn (user + agent), a summary, or a note. Episodes are stored
 first and durably; the cognify worker extracts facts afterwards.

 Examples:
 astoria capture --text "Decided to pin vLLM 0.20.2 on the RDNA4 box"
 astoria capture --user-input "I moved to Portland" --agent-response "Noted!" --session s1
 astoria capture --kind summary --text "..." --session s1 --priority high
 cat notes.txt | astoria capture --stdin --no-cognify

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --text                    <str>    Free text (note / summary).                                   │
│ --user-input              <str>    User side of a turn.                                          │
│ --agent-response          <str>    Agent side of a turn.                                         │
│ --kind                    <str>    turn | summary | note (turn when both sides are given).       │
│                                    [default: note]                                               │
│ --session         -s      <str>    Session id.                                                   │
│ --at                      <str>    When it happened (default now).                               │
│ --importance              <float>  0..1 (default .5).                                            │
│ --tags                    <str>    Comma-separated tags.                                         │
│ --no-cognify                       Store the episode but don't queue fact extraction.            │
│ --priority                <str>    normal | high (jump the queue). [default: normal]             │
│ --stdin                            Read --text from standard input.                              │
│ --help            -h               Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria episodes`

```text

 Usage: astoria episodes [OPTIONS]

 List episodes (working memory turns, summaries, notes, imports).

 Examples:  astoria episodes --kind summary  ·  astoria episodes --session s1 -n 50

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Filter by session id.                                                  │
│ --kind             <str>  turn | summary | note | import                                         │
│ --limit    -n      <int>  Max rows (newest first). [default: 30]                                 │
│ --help     -h             Show this message and exit.                                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria episode delete`

```text

 Usage: astoria episode delete [OPTIONS] {episode_id}

 Delete one episode (facts already extracted from it are kept).

 Example:  astoria episode delete 7c0e...

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    episode_id      <str>  Episode id (full UUID). [required]                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --yes   -y        Skip confirmation.                                                             │
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria predicates`

```text

 Usage: astoria predicates [OPTIONS]

 List the predicate registry: cardinality (functional = one current value, set = many) and layer
 hint.

 Examples:  astoria predicates  ·  astoria predicates --auto

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --auto            Only auto-registered predicates (created by the extractor — review their       │
│                   cardinality).                                                                  │
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria predicate set`

```text

 Usage: astoria predicate set [OPTIONS] {name}

 Set a predicate's cardinality and/or layer hint (PATCH /predicates/NAME).

 Examples:
 astoria predicate set favorite_beer --functional
 astoria predicate set likes --set --layer profile

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    name      <str>  Predicate name (snake_case). [required]                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --functional      --set           Cardinality: one current value / many values.                  │
│ --layer                    <str>  Layer hint: semantic | profile | procedural.                   │
│ --help        -h                  Show this message and exit.                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria audit`

```text

 Usage: astoria audit [OPTIONS]

 Audit log for the user — who asserted / retracted / forgot / approved what, when.

 Example:  astoria audit -n 100

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --limit  -n      <int>  Max rows (newest first). [default: 50]                                   │
│ --help   -h             Show this message and exit.                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria queue`

```text

 Usage: astoria queue [OPTIONS]

 Cognify queue stats (pending / dead / in-flight). Uses POST /op queue_stats and falls back to the
 queue block of /health.

 Example:  astoria queue

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria wipe-user`

```text

 Usage: astoria wipe-user [OPTIONS] {user_id}

 DANGEROUS: erase everything Astoria knows about USER_ID (DELETE /users/{user}). Requires --yes and
 a typed confirmation.

 Example:  astoria wipe-user test-user --yes

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    user_id      <str>  The user to erase — ALL facts, episodes, profile. [required]            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --yes              Required. Acknowledge this is destructive.                                    │
│ --force            Skip the interactive typed confirmation (for scripts/tests).                  │
│ --help   -h        Show this message and exit.                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria graph`

```text

 Usage: astoria graph [OPTIONS] {node}

 Walk the memory graph around NODE: a tree of reachable entities/facts with the relation of each
 hop (GET /graph). Aliases resolve to their canonical entity.

 Examples:  astoria graph buildbot  ·  astoria graph workstation-1 --depth 1

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    node      <str>  Entity name (e.g. buildbot), 'entity:NAME' or 'fact:UUID'. [required]      │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --depth   -d      <int range> [0<=x<=6]  Hops to walk (undirected). [default: 2]                 │
│ --fanout          <int>                  Max edges followed per node per hop.                    │
│ --help    -h                             Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria edges`

```text

 Usage: astoria edges [OPTIONS]

 List graph edges (GET /edges): src —relation→ dst with weight, confidence, provenance.

 Examples:  astoria edges  ·  astoria edges --node buildbot --depth 1
 ·  astoria edges --relation runs_on

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --node      -N      <str>                  Only edges touching this node (entity name /          │
│                                            entity:NAME / fact:UUID).                             │
│ --relation  -r      <str>                  Only this relation (snake_case).                      │
│ --depth     -d      <int range> [0<=x<=6]  With --node: also edges within DEPTH hops.            │
│                                            [default: 0]                                          │
│ --status            <str>                  active | retracted | archived | superseded | any      │
│                                            [default: active]                                     │
│ --limit     -n      <int>                  [default: 200]                                        │
│ --help      -h                             Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria edge add`

```text

 Usage: astoria edge add [OPTIONS] {src} {relation} {dst}

 Assert an edge SRC —RELATION→ DST (POST /edges). Idempotent: re-adding an active edge bumps it.
 Entity endpoints are auto-registered; aliases resolve to their canonical entity.

 Example:  astoria edge add buildbot runs_on workstation-1

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    src           <str>  Source node: entity name, entity:NAME or fact:UUID. [required]         │
│ *    relation      <str>  snake_case relation: part_of, located_in, works_at, owns, runs_on,     │
│                           depends_on, related_to ...                                             │
│                           [required]                                                             │
│ *    dst           <str>  Destination node. [required]                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --weight      -w      <float>  Edge weight (default 1).                                          │
│ --confidence          <float>  0-1 (default .90 explicit).                                       │
│ --evidence            <str>    Verbatim support snippet.                                         │
│ --from                <str>    Valid-from date.                                                  │
│ --to                  <str>    Valid-to date.                                                    │
│ --help        -h               Show this message and exit.                                       │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria edge rm`

```text

 Usage: astoria edge rm [OPTIONS] {edge_id}

 Retract (default) or hard-delete an edge (DELETE /edges/ID).

 Example:  astoria edge rm 3f2a1c9b

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    edge_id      <str>  Edge id (full uuid or unique prefix as printed by `edges`). [required]  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --hard            Delete the row instead of retracting it.                                       │
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria alias add`

```text

 Usage: astoria alias add [OPTIONS] {alias} {canonical}

 Declare ALIAS to mean CANONICAL (POST /aliases): every later write/read on ALIAS lands on
 CANONICAL. Chains are flattened; the user_id itself cannot be aliased away.

 Example:  astoria alias add ws1 workstation-1

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    alias          <str>  The other name. [required]                                            │
│ *    canonical      <str>  The name to keep (what fact.subject holds). [required]                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria alias list`

```text

 Usage: astoria alias list [OPTIONS]

 List subject aliases (GET /aliases).

 Example:  astoria alias list  ·  astoria alias list -c workstation-1

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --canonical  -c      <str>  Only aliases of this canonical name.                                 │
│ --help       -h             Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria alias rm`

```text

 Usage: astoria alias rm [OPTIONS] {alias}

 Remove an alias (DELETE /aliases/ALIAS). Facts already written under the canonical name stay.

 Example:  astoria alias rm ws1

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    alias      <str>  The alias to remove. [required]                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria export`

```text

 Usage: astoria export [OPTIONS]

 Dump facts + episodes for the user to JSON (via the list endpoints, paginated). Pair with import
 to move memory between instances.

 Examples:
 astoria export -o alice-$(date +%F).json
 astoria --user bob export --status active --no-episodes > bob-facts.json

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --out            -o      <path>  Write to this file (default stdout).                            │
│ --status                 <str>   Which facts: any (default, keeps history) | active | …          │
│                                  [default: any]                                                  │
│ --no-episodes                    Facts only.                                                     │
│ --page                   <int>   Page size for /facts pagination. [default: 500]                 │
│ --episode-limit          <int>   Max episodes to pull. [default: 10000]                          │
│ --help           -h              Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria import`

```text

 Usage: astoria import [OPTIONS] {file}

 Replay an export into the target user via POST /facts (explicit; valid window, layer, cardinality,
 tags, confidence preserved where the API allows). Simple, idempotent enough to re-run: identical
 triples are no-ops server-side.

 Examples:
 astoria import alice-2026-08-22.json
 astoria --user alice-test import alice.json --all --episodes --dry-run

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    file      <file>  JSON file written by astoria export. [required]                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --all                 Also replay superseded/retracted facts as historical (default: active      │
│                       only).                                                                     │
│ --episodes            Also replay summary/note episodes via /capture (no cognify).               │
│ --dry-run             Show what would be sent.                                                   │
│ --help      -h        Show this message and exit.                                                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```
