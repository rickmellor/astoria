# Astoria — CLI (`astoria`)

`astoria` is a thin typer/rich client over the REST API ([API.md](API.md)). It never touches the
database; everything it does you can also do with `curl`. Installed with the package
(`pip install -e .` / `pipx install .` from `~/repos/astoria`; entry point `astoria = astoria.cli.main:app`).

## Setup

```bash
cd ~/repos/astoria && . .venv/bin/activate        # or: pipx install ~/repos/astoria
set -a; . ~/.config/astoria/env; set +a            # ASTORIA_URL + ASTORIA_TOKEN (mode 600) on the workstation
astoria status                                     # exit 0 iff the service reports ok
astoria --install-completion                       # optional shell completion
```

| env var | default | meaning |
|---|---|---|
| `ASTORIA_URL` | `http://192.168.1.134:8933` | service base URL (`--url`) |
| `ASTORIA_TOKEN` | – | sent as `Authorization: Bearer`; maps to the client name `cli` server-side (trust cap 1.0) (`--token`) |
| `ASTORIA_USER` | `rick` | `user_id` every request is scoped to (`--user/-u`) |

Global flags go **before** the subcommand: `astoria --json facts …`, `astoria -u bob profile`,
`astoria --timeout 60 export`. `--json/-j` prints the raw API response (pipe into `jq`). Short fact ids
(the first 8 chars shown in tables) are accepted wherever an id is expected (`fact`, `approve`,
`retract --id`, `forget --id`). Dates accept `YYYY-MM-DD`, ISO-8601, or `now` / `today` / `yesterday` /
`3 days ago` / `2 weeks ago`.

## Workflows

```bash
# what does memory know about X?  (prints the context block + ranked table)
astoria recall "what editor do I use"
astoria recall "deploy steps" --layers procedural,semantic --facts-only
astoria recall "beer" --context-only >> prompt.txt          # just the injectable block

# state a durable fact (explicit, conf .90); functional predicates supersede, set predicates add
astoria remember rick favorite_beer IPA
astoria remember rick uses_tool Neovim --set
astoria remember rick employer Acme --from 2019-01-01 --to 2023-12-31 --historical   # past value, current untouched
astoria correct  rick favorite_beer Stout                    # prints what it superseded
astoria history  rick favorite_beer                         # the chain as a timeline
astoria as-of 2026-07-01 --predicate favorite_beer          # what was true then
astoria as-of 2026-07-01 --believed-at 2026-07-01           # ...as the system believed it then

# stop believing / forget
astoria retract rick uses_tool Emacs                        # status retracted, history kept, tombstoned
astoria forget "old address"                                # preview → confirm (soft)
astoria forget --id 3f2a9c1e --hard --yes

# free text → episode → cognify extracts facts in the background
astoria remember --text "Rick prefers dark mode and tabs over spaces"
astoria capture --kind summary --text "…" --session s1 --priority high
cat notes.txt | astoria capture --stdin --no-cognify

# review what the extractor produced
astoria staging && astoria approve 3f2a9c1e 9b1d
astoria predicates --auto                                   # auto-registered predicates: check cardinality
astoria predicate set collects_x --set --layer profile
astoria facts -q beer --status any ; astoria fact 3f2a      # browse, then provenance + chain
astoria audit -n 100 ; astoria queue ; astoria episodes --kind summary

# move memory between instances / keep a portable copy
astoria export -o rick-$(date +%F).json
astoria --user rick-test import rick-2026-08-22.json --all --episodes --dry-run
```

Notes:
- `forget` soft = archived + tombstoned (hidden from recall, recoverable via `PATCH status=active`
  / `astoria --json` + `/op fact_update`); `--hard` deletes the row. Both block re-extraction of the
  same triple until an explicit `remember` lifts the tombstone.
- `queue` tries `POST /op queue_stats` first; that action is not in the v1 dispatcher, so it falls back
  to the `queue` block of `/health` (pending / dead / by_state) — expected, not an error.
- `wipe-user` is destructive (`DELETE /users/{id}`): requires `--yes` and a typed confirmation
  (`--force` for scripts). Use it on throwaway users only.
- `import` replays facts via `POST /facts` as explicit (so the `source` becomes the importing client and
  confidence is preserved where the API allows); identical triples are server-side no-ops, so re-running
  is safe. `--all` replays superseded/retracted rows as historical; `--episodes` replays summary/note
  episodes with cognify off.

## Test-drive script (safe: throwaway user, wiped at the end)

```bash
#!/usr/bin/env bash
# astoria test drive — exercises the control plane end to end against the live NAS service.
set -euo pipefail
set -a; . ~/.config/astoria/env 2>/dev/null || true; set +a
export ASTORIA_USER="drive-$$"
a() { echo; echo "\$ astoria $*"; astoria "$@"; }

a status
a remember "$ASTORIA_USER" favorite_beer Guinness
a remember "$ASTORIA_USER" favorite_beer IPA                # supersedes Guinness
a remember "$ASTORIA_USER" likes stout --set
a remember "$ASTORIA_USER" likes IPA --set
a remember "$ASTORIA_USER" default_johnny_profile coder --from 2026-07-01 --to 2026-08-18 --historical
a remember "$ASTORIA_USER" default_johnny_profile daily --from 2026-08-18
a facts --status any
a history "$ASTORIA_USER" favorite_beer
a as-of 2026-07-15 --predicate default_johnny_profile       # → coder
a as-of today --predicate default_johnny_profile            # → daily
a recall "what beer do I like" --facts-only                 # IPA first, Guinness never
a capture --text "Actually, my favorite beer is stout" --no-cognify   # detector → favorite_beer=stout
a facts --predicate favorite_beer
a retract "$ASTORIA_USER" likes stout
a forget "IPA" --yes                                        # soft-forget the best match
a audit -n 20
a --json briefing | head -c 400; echo
a wipe-user "$ASTORIA_USER" --yes --force
echo; echo "test drive OK"
```

Expected: every step prints a table/panel and exits 0; `as-of 2026-07-15` shows `coder`, `recall`
shows `IPA` on top and never `Guinness`, the capture shows `detector → favorite_beer=stout
(superseded)`, and the final wipe reports the deleted row counts. For a scripted post-deploy check use
`scripts/smoke.sh` (health + MCP handshake + the T1 correction test).

## Full help (`astoria --help` and every subcommand)

Generated from the installed CLI on 2026-08-22 (`COLUMNS=100`). Re-generate with
`astoria --help; astoria <cmd> --help`.

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
   astoria remember rick favorite_beer IPA           → assert a fact (supersedes the old value)     
   astoria remember --text "Prefers dark mode"       → capture a note; the worker extracts facts    
   astoria correct rick favorite_beer Stout          → same as remember, shows what it replaced     
   astoria facts -q beer · astoria fact 3f2a        → browse, then inspect provenance               
   astoria history rick favorite_beer                → supersede chain as a timeline                
   astoria as-of 2026-01-01                          → what was true back then                      
   astoria staging → astoria approve ID               → review extracted facts                      
   astoria briefing · astoria profile                  → stable prompt prefix / who the user is     
                                                                                                    
 Environment                                                                                        
   ASTORIA_URL    service base URL   (default http://192.168.1.134:8933)                            
   ASTORIA_TOKEN  bearer token → client name server-side (sent as Authorization: Bearer)            
   ASTORIA_USER   default user_id    (default rick)                                                 
                                                                                                    
 Short fact ids (first 8 chars, as printed in tables) are accepted anywhere an ID is expected.      
 Dates accept YYYY-MM-DD, ISO-8601, or "now" / "today" / "yesterday" / "3 days ago" / "2 weeks      
 ago".                                                                                              
                                                                                                    
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --user                -u      <str>    user_id every request is scoped to.                       │
│                                        [env var: ASTORIA_USER]                                   │
│                                        [default: rick]                                           │
│ --url                         <str>    Service base URL.                                         │
│                                        [env var: ASTORIA_URL]                                    │
│                                        [default: http://192.168.1.134:8933]                      │
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
│ correct     Correct a fact — same as remember (POST /correct) but prints what it superseded.     │
│ retract     Retract a fact: it stops being true now (status → retracted, history kept,           │
│             tombstoned so extraction can't resurrect it).                                        │
│ forget      Forget facts — stronger than retract: they vanish from history too (soft = hidden,   │
│             --hard = gone). Query mode shows the matches and asks before acting.                 │
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
 astoria remember rick favorite_beer IPA                                                            
 astoria remember rick uses_tool Neovim --set                                                       
 astoria remember rick lives_in Portland --from 2024-06-01                                          
 astoria remember rick employer Acme --from 2019-01-01 --to 2023-12-31 --historical                 
 astoria remember --text "Rick prefers dark mode and tabs over spaces"                              
                                                                                                    
╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   subject        <str>  Subject ('rick' / 'I' / 'me' → the user).                                │
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

### `astoria correct`

```text
                                                                                                    
 Usage: astoria correct [OPTIONS] {subject} {predicate} {value}                                     
                                                                                                    
 Correct a fact — same as remember (POST /correct) but prints what it superseded.                   
                                                                                                    
 Example:  astoria correct rick favorite_beer Stout                                                 
                                                                                                    
╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│ *    subject        <str>  Subject. [required]                                                   │
│ *    predicate      <str>  Predicate. [required]                                                 │
│ *    value          <str>  The new, correct value. [required]                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --from          <str>  When the new value became true.                                           │
│ --help  -h             Show this message and exit.                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria retract`

```text
                                                                                                    
 Usage: astoria retract [OPTIONS] [subject] [predicate] [value]                                     
                                                                                                    
 Retract a fact: it stops being true now (status → retracted, history kept, tombstoned so           
 extraction can't resurrect it).                                                                    
                                                                                                    
 Examples:                                                                                          
 astoria retract rick favorite_beer         (all values of the key)                                 
 astoria retract rick uses_tool Emacs       (one value)                                             
 astoria retract --id 3f2a9c1e                                                                      
                                                                                                    
╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   subject        <str>  Subject.                                                                 │
│   predicate      <str>  Predicate.                                                               │
│   value          <str>  Value (omit to retract every value of the key).                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --id            <str>  Retract by fact id instead.                                               │
│ --help  -h             Show this message and exit.                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯

```

### `astoria forget`

```text
                                                                                                    
 Usage: astoria forget [OPTIONS] [query]                                                            
                                                                                                    
 Forget facts — stronger than retract: they vanish from history too (soft = hidden, --hard = gone). 
 Query mode shows the matches and asks before acting.                                               
                                                                                                    
 Examples:                                                                                          
 astoria forget --id 3f2a9c1e                                                                       
 astoria forget "old address"           (preview → confirm)                                         
 astoria forget "old address" --hard --yes                                                          
                                                                                                    
╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   query      <str>  Search text; matching facts are listed first.                                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --id             <str>  Forget exactly this fact id.                                             │
│ --hard                  Hard delete (row removed). Default is soft (status=deleted, recoverable, │
│                         tombstoned).                                                             │
│ --yes    -y             Skip the confirmation prompt.                                            │
│ --limit          <int>  (query mode) max matches to show/forget. [default: 25]                   │
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
 astoria facts --subject rick --layer profile                                                       
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
                                                                                                    
 Example:  astoria history rick favorite_beer                                                       
                                                                                                    
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
 astoria as-of "1 year ago" --subject rick --predicate lives_in                                     
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

### `astoria predicate`

```text
                                                                                                    
 Usage: astoria predicate [OPTIONS] COMMAND [ARGS]...                                               
                                                                                                    
 Operate on a single predicate (set cardinality/layer). Use astoria predicates to list.             
                                                                                                    
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                                    │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────────────────╮
│ set  Set a predicate's cardinality and/or layer hint (PATCH /predicates/NAME).                   │
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

### `astoria export`

```text
                                                                                                    
 Usage: astoria export [OPTIONS]                                                                    
                                                                                                    
 Dump facts + episodes for the user to JSON (via the list endpoints, paginated). Pair with import   
 to move memory between instances.                                                                  
                                                                                                    
 Examples:                                                                                          
 astoria export -o rick-$(date +%F).json                                                            
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
 astoria import rick-2026-08-22.json                                                                
 astoria --user rick-test import rick.json --all --episodes --dry-run                               
                                                                                                    
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
