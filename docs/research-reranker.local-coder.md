# Reranker for Astoria's second recall stage — research brief

## Bottom line

- **Run BAAI/bge-reranker-v2-m3 — or the near-twin gte-multilingual-reranker-base — on the specul8 RTX 4080 (TEI image `89-1.9`), upgrading the existing specul8 CPU rerank seat; keep the NAS MiniLM-L6 seat as the always-on fallback.** `ASTORIA_RERANK_URLS` is already a priority list of `url|model` pairs [52], and a specul8 CPU reranker seat (port 8935, `--cpus 8`, serving the same MiniLM model (I)) already runs and is preferred over the NAS one [63], so adding/replacing the GPU seat degrades gracefully at night with no code change (placement is inference (I); list format and degrade-don't-fail path are as documented [52][57]).
- **Two candidates clear every gate at once** (≤1.2 GB fp16, Apache-2.0, multilingual, TEI-routable) and differ mostly in provenance: **bge-reranker-v2-m3** (567,755,777 params, fp32-only checkpoint → ~1.14 GB fp16 (I) [1]) is already on the NAS model store; its XLM-RoBERTa seq-class architecture is the one TEI supports for reranking [34] (v2-m3 itself is not in TEI's example table (I)). **gte-multilingual-reranker-base** (305,959,681 params → ~610 MB fp16 (I) [20]) is explicitly listed in TEI's reranker example table [34], is "over 70 languages" [20], and scores *higher* than bge on the same Qwen3 protocol (MTEB-R 59.51 vs 57.03; MLDR 66.33 vs 59.51 [7]) — but needs a one-time HF download. Both are drop-in changes of `ASTORIA_RERANK_URLS` [52][53]; pick bge for zero download, gte for the published edge (I: negligible in practice).
- The highest-quality ≤1.5 GB model is **Qwen3-Reranker-0.6B** (MTEB-R 65.80 vs bge-v2-m3 57.03; 32 K context; 100+ languages; Apache-2.0 [5][7]) — but TEI cannot serve it (support PRs unmerged, stock config lacks `id2label` [35][36]); it needs vLLM or llama.cpp, and its per-query latency is unpublished (Gaps).
- **Skip jina v2/v3** despite v3's top published BEIR (61.94 nDCG@10): both are **CC-BY-NC-4.0, non-commercial** [14][15]. **Skip mxbai-rerank-large-v2** (1.54 B params, 2.88 GB fp16 — over the 1.5 GB budget) [11]. mxbai-base-v2 (494 M, 0.94 GB [10]) fits and scores *above* bge-v2-m3 on mxbai's own BEIR table (BM25 first stage: 55.57 vs 53.94) [13] — but bge's 57.03 is an MTEB-R figure from the Qwen3 protocol [7], not that table; the vendor tables use different first-stage retrievers and must not be merged [13][15].
- Expected trade: ~100–250 ms to rerank 50 candidates of ~200 tokens on the 4080 (I, scaled from 90 ms/100 pairs on H100 with 4090 ≈ 50 % of H100 [49]), versus the measured MiniLM cost of ~324 ms p50 per 30-hook call on the NAS CPU [55]. Quality gains from reranking are real but modest: Astoria's own eval showed MRR 0.765→0.814 and avoid@5 0.294→0.118 with MiniLM [55].

## Context

Astoria recall is nomic-embed bi-encoder search via TEI on the NAS :8931, with an optional `astoria-rerank` TEI service on :8935 wired through `ASTORIA_RERANK_URLS`, currently sized for `cross-encoder/ms-marco-MiniLM-L-6-v2` (22.7 M params, 240-char texts, top-30, weight 0.6, 3 s timeout) [52][55][56]. Constraint: the NAS (UGREEN DXP4800 Plus, Intel Pentium Gold 8505 5-core/6-thread, no GPU [48]) is always on; specul8 (RTX 4080 16 GB, shared with a ~3 GB sidecar and a 13.8 GB llama.cpp judge) **powers off nightly** [59][60]. This brief answers which reranker to use and where, written only from the evidence pack of 2026-09-16/23.

## Findings

### 1. Candidates (≤1.5 GB GPU or acceptable on CPU)

| Model | Params | Size (as shipped / quant) | Max seq | Multilingual | Licence |
|---|---|---|---|---|---|
| cross-encoder/ms-marco-MiniLM-L6-v2 | 22.7 M [17] | ~45 MB fp32 (I) / FlashRank ONNX ~34 MB (MiniLM-L12) [47] | 512 [17][18] | English | Apache-2.0 [17] |
| gte-reranker-modernbert-base | 149 M [19] | ~300 MB fp16 (I) | 8192 [19] | English | Apache-2.0 [19] |
| granite-embedding-reranker-english-r2 | 149 M [21] | — | 8192 [21] | English | Apache-2.0 [21] |
| gte-multilingual-reranker-base (mGTE) | 305,959,681 [20] (paper: 304 M [22]) | ~610 MB fp16 (I) | 8192 [20][22] | "over 70 languages" (75 tagged) [20] | Apache-2.0 |
| jina-reranker-v2-base-multilingual | 278,437,633 [14] | 556,892,306 B BF16 [14] | 1024 [14] (8192 in m0-blog table — disagreement) | 100+ [14] | **CC-BY-NC-4.0** [14] |
| BAAI/bge-reranker-v2-m3 | 567,755,777 [1] | 2.27 GB fp32 only; fp16 is a runtime flag [1][2] → ~1.14 GB fp16 (I) | 8194 (`max_position_embeddings` [1]; card example truncates at 512 [2]; evals ran at 1024 [2]) | Multilingual (bge-m3 backbone, 100+ languages [3]) | Apache-2.0 [1] |
| Qwen3-Reranker-0.6B | 595,776,512 [5] | 1,191,588,280 B BF16 [5]; community GGUF Q8_0 0.7 GB / f16 1.3 GB [8] | 32 k [5][7] | 100+ [5] | Apache-2.0 [5] |
| jina-reranker-v3 | 596,836,352 [15] | 1,193,708,120 B BF16 [15]; GGUF/MLX exist, sizes unpublished [15] | 131 k, ≤64 docs per pass (listwise) [15][16] | 100+ [15] | **CC-BY-NC-4.0** [15] |
| mxbai-rerank-base-v2 (ProRank-0.5B) | 494,032,768 [10] | 988,097,536 B fp16 [10] | 8 K (32 K-compatible) [13] | 109 [10] | Apache-2.0 [13] |
| mxbai-rerank-large-v2 (ProRank-1.5B) | 1,543,714,304 [11] (page says "2B" — disagreement) | 3,087,466,808 B fp16 [11] | 8 K (32 K-compatible) [13] | 100+ [13] | Apache-2.0 [13] |
| Qwen3-Reranker-4B | 4,021,784,576 [6] | 7.48 GB BF16 [6]; GGUF Q8_0 4.4 GB [8] | 32 k [6] | 100+ | Apache-2.0 — **over budget** |
| zeroentropy/zerank-1-small | 1,720,574,976 [24] | bytes not in pack (Gaps) | — | — | Apache-2.0 [24] |

Notes: bge-reranker-base/large (v1) are 278 M/560 M, MIT, but Chinese+English only and the large card defers to the v2 rerankers [4]. mxbai-rerank-large-v1 (435 M params, 830 MB fp16, 512 tokens, BEIR 48.8 under the Pyserini protocol) is superseded by v2 [9][12]. jina-reranker-m0 (2.4 B, multimodal) is out of scope for text recall [15].

### 2. Retrieval-quality evidence

- **Qwen3 protocol (top-100 from Qwen3-Embedding-0.6B bi-encoder)** [7]: Qwen3-Reranker-0.6B MTEB-R **65.80** / CMTEB-R 71.31 / MMTEB-R 66.36 / MLDR 67.28 / MTEB-Code 73.42; baselines: gte-multilingual **59.51** (MLDR 66.33), jina-v2-base 58.22, bge-v2-m3 **57.03** (MLDR 59.51) — same protocol, so within this table gte > jina-v2 > bge; bi-encoder alone 61.82 → 0.6B reranker **+3.98**, 8B +7.20 on MTEB-R [7]. mGTE's own paper (304 M, 8192 tokens, 70+ languages) reports BEIR 55.4 / MLDR 67.2 under its own protocol [22]. "All three Qwen3-Reranker models enhance performance compared to the embedding model and surpass all baseline reranking methods" [7].
- **bge-v2-m3 (OCR of the card's PNG tables — no text version exists [2])**: BEIR 55.36 nDCG@10 reranking bge-en-v1.5-large top-100 vs 54.31 with no reranker; MIRACL 72.84 vs 67.91 baseline; llama-index eval 78.26–80.76 vs 65.07 "without reranker".
- **Cross-vendor BEIR comparisons disagree and must not be merged**: mxbai's v2 blog puts large-v2 57.49 > cohere 55.39 > jina-v2 54.35 > bge-v2-m3 53.94 [13]; jina's v3 card lists mxbai-large-v2 at 61.44 and bge-v2-m3 at 56.51, with v3 itself at 61.94 (arXiv full text says 61.85) [15][16]; its own v2 card says v2 = 53.17 while the v3 card says 57.06 [14][15].
- **2025–2026 leaders**: Agentset leaderboard (2026-02-15) — Zerank 2 #1 (1638 ELO), Cohere Rerank 4 Pro #2, Zerank-1-Small 1539, Qwen3-Reranker-8B 1473; "Top rerankers deliver 15-40% higher precision than embeddings alone" [23]. RankArena's unified reranker evaluation tops out at 52.8 average BEIR (twolar) — a different scale from the vendor tables [25]. zerank-1-small (Apache-2.0, 1.7 B) shows the largest single-domain jumps on OpenAI-embedding top-100: Conversational 0.250→0.556, Medical 0.619→0.796 nDCG@10 [24] — the closest proxy to memory-style data in the pack.
- **Memory / personal-knowledge corpora**: Zep (production agent memory) uses BGE-m3 "for both reranking and embedding tasks", cross-encoder as the top-tier stage [26]; Cognis (BGE-2 cross-encoder after BM25+vector RRF 70/30 + temporal boost) adds "approximately 20-50ms latency" and reports LoCoMo 48.66/31.51/54.77/62.68 F1; 22 % of its temporal failures were **reranker ordering errors** — a reminder that a reranker doesn't fix temporal ranking alone [27]; Mem0's multi-signal fused score (semantic+BM25+entity) gained +29.6 points on temporal queries with an explicit second-pass reranker stage [28]; LongMemEval: correct answers require correct retrieval ~90 % of the time, and dense retrieval beats BM25 on its chat-history corpus [29]; LoCoMo notes generic similarity retrieval is "compromised" on dialogue [61]. T²-RAGBench (text+table RAG): hybrid+rerank Recall@5 **0.816** vs 0.695 hybrid-only (+17.4 %) and 0.587 dense-only (+39 %); candidate-pool depth matters — 20 candidates "reranking is ineffective" (0.458), 50 → 0.826, 100 → 0.888 [33].
- No clean with-vs-without-reranker A/B on LoCoMo/LongMemEval exists in the pack (Gaps).

### 3. Latency & serving (20–50 candidates, ~200 tokens)

Measured: H100 batched, 512-token docs [49]: bge-v2-m3 **1100 pairs/s, 90 ms/100 pairs**; jina-v2 3500 pairs/s, 28 ms; mxbai-rerank-base 4200 pairs/s, 24 ms. aimultiple H100 [50]: jina-v3 188 ms/100 docs; nemotron-1b 243 ms; **Qwen3-Reranker-4B >1 s/query** (0.6B value in a chart only). Astoria's own seats [55][56]: NAS CPU MiniLM ~324 ms p50 per 30-hook call (240-char texts, ONNX ≈ 11 ms/hook, ~0.3 ms/token [52][56]); the existing specul8 CPU TEI seat — the reranker the ops script says Astoria prefers over the NAS one [63] — does 116.4 ms p50 / ~12 calls/s vs 323.5 ms / ~2.9 calls/s on the NAS [55]. MiniLM-L6 reference: ~12 ms (1 doc) to ~740 ms (100 docs) CPU; 30–50 ms GPU / 100–200 ms CPU for ~30 candidates [51].

Inferences for our hardware: 4090 ≈ 50 % of H100 [49] → 4080 (slightly below 4090 (I)): **~100–250 ms for 50 pairs** with bge-v2-m3 (I). NAS-class CPU: Ryzen 7 7800X3D is "1/30th of H100 (use only for <100 queries/day)" [49]; the DXP4800 Plus's Pentium Gold 8505 (5C/6T [48]) is well below that → bge-v2-m3 on NAS CPU is likely **multiple seconds per 50 pairs — beyond the 3 s `ASTORIA_RERANK_TIMEOUT_S`** (I). MiniLM-class on NAS CPU is the measured ~300 ms (I consistent with [51][55]). GPU memory: bge fp16 ≈1.14 GB (I) and Qwen3-0.6B ≈1.19 GB [5] fit a ~1.5 GB slice, but the 4080's 16 GB is already shared with a 3 GB sidecar + 13.8 GB judge, so the reranker seat only coexists when the judge is not resident, or must use a low `gpu_memory_utilization` (default 0.92) / llama.cpp's lighter footprint (I) [40].

Serving options (pack evidence):

- **Hugging Face TEI (v1.9.4)**: "Re-rankers models are Sequence Classification cross-encoders models with a single class that scores the similarity" [34]; it "currently supports CamemBERT, and XLM-RoBERTa Sequence Classification models with absolute positions" [34], with example rows for bge-reranker-base/large (XLM-RoBERTa), gte-multilingual-reranker-base (GTE) and gte-reranker-modernbert-base (ModernBERT) [34] → **bge-v2-m3 (XLM-RoBERTa, v1 siblings listed) should serve (I); MiniLM is proven in service on the Astoria NAS seat [55][56]; mxbai-large-v2 (XLM-RoBERTa classifier) worked per closed issue #532 (fix not citable) [38]**. **Qwen3-Reranker: unsupported** — open issues #763/#795/#835, PR #886 unmerged, stock config lacks `id2label`/`label2id` [35][36]. jina-v3: PR #737 closed abandoned [37]. mxbai-base-v2 is Qwen-2.5-based → not a supported TEI arch (I) [10]. Images: `cpu-arm64-1.9` (aarch64), `89-1.9` (Ada/RTX 40xx); knobs `--max-batch-tokens` (default 16384), `--max-client-batch-size 32` [34].
- **vLLM**: `/v1/rerank` Jina/Cohere-compatible; natively lists `XLMRobertaForSequenceClassification` ("BAAI/bge-reranker-v2-m3, etc."); Qwen3-Reranker-0.6B served via `hf_overrides` (seq-classifier + yes/no logit diff); mxbai-base-v2 via Qwen2 override; jina-m0 direct [39]. Caveat: issue #22048 reports poor concurrency ("requests are still processed serially") [41]; `gpu_memory_utilization` default 0.92 [40].
- **llama.cpp**: `/reranking` endpoint with `--reranking`; bge-v2-m3 tested at 1941 tok/s on Apple Silicon Metal [42][43]; Qwen3-Reranker supported since merged PR #15824 (use `--embd-normalize -1`) [44]. Lightest memory footprint of the three — the safe 4080 co-tenant option (I).
- **sentence-transformers / ONNX**: CrossEncoder with PyTorch/ONNX/OpenVINO backends, "up to 2x-3x" speedups claimed; fp16 "still pays off 2 to 3x" on GPU (RTX 3090); "ONNX and OpenVINO can even perform slightly worse than PyTorch" on CPU; torch.compile `reduce-overhead` for batch-1 [45]. int8 (S8S8/QDQ default) gains are hardware-dependent: "Old hardware has none or few of the instructions needed" [46] — relevant to the 8505 (I). FlashRank ships ready CPU ONNX cross-encoders (MiniLM-L12 ~34 MB) with no runtime [47].

### 4. How Astoria's pipeline uses it, and NAS-vs-specul8

Pipeline: `query → embed → candidates → RRF → weight → graph expansion → **rerank** → collapse → budget → context` [54]. Stage: top-`ASTORIA_RERANK_TOP_N` (30) facts + top-6 episodes, texts capped at 240 chars, scored as `(query, text)` logits; blend `final = (1-w)·norm(base) + w·norm(sigmoid(logit))` with `w=0.6`, min-max over the reranked set [52][58]. Degrade-don't-fail: timeout 3 s, 60 s failure cooldown, 600 s wrong-model cooldown, `rerank()` never raises, `health.rerank` = on/off/down [57]. Measured MiniLM result: MRR 0.765→0.814, avoid@5 0.294→0.118, precision@5 0.494→0.459 (slight loss), cold p50 89→585 ms; 8-client warm p95 493 vs 500 ms; NAS seat caps recall at <3 req/s [55].

Design guidance (mixed fact/inference):

- **Pool size 30–50 is the sweet spot**: T²-RAGBench shows 20 candidates too small (0.458), 50 strong (0.826) [33]; Astoria's default 30 facts + 6 episodes already lands there [52] — a GPU reranker justifies raising `TOP_N` toward 50, per the docs' own verdict ("a GPU reranker allows a larger TOP_N") [53].
- **Fusion**: keep the existing min-max sigmoid blend (proven [55]); recency/importance belong at the weight stage *before* rerank, matching memory-system practice: MemX RRF + four-factor re-rank (semantic 0.45, recency 0.25, importance 0.10, frequency 0.05; recency half-life 30 days, RRF k=60) [31][32]; Generative Agents min-max normalises recency (exponential decay)/relevance/importance with equal α [30]. Watch Cognis's finding that 22 % of temporal failures were reranker mis-ordering [27] — a temporal/recency bias should survive the rerank blend.
- **NAS vs specul8**: the NAS is always-on [59]; specul8 (RTX 4080 16 GB) powers off nightly [60]. GPU seat on specul8 = ~100–250 ms/50 candidates (I) but unavailable overnight; NAS CPU can only host MiniLM-class models within the 3 s timeout (I). The contract already handles this: `ASTORIA_RERANK_URLS` is a **priority list** — first endpoint that answers `GET /info` and matches a served-model hint wins, with 60 s cooldown on failure [52][57]. So: bge (or gte) on specul8 first, NAS MiniLM second → no availability regression, quality upgrade by day (I). The existing compose already pins the TEI image, 1 GiB mem_limit, `--auto-truncate`, port 8935 [56]. The specul8 seat is **not new**: an existing CPU TEI rerank seat is already documented — the ops script comment "(Re)create the workstation TEI cross-encoder reranker that Astoria prefers over the NAS one" and `docker run -d --name astoria-rerank ... --cpus 8` on port 8935 [63] — and it is preferred over the NAS one (116.4 vs 323.5 ms p50 per 30-hook call, ~12 vs ~2.9 calls/s) [55][63]. What does not exist yet is a GPU (4080) seat: no infra doc or compose deploys TEI rerank on the 4080, which currently runs scanforge [60] — the upgrade is replacing the existing specul8 CPU seat with a `89-1.9` GPU seat serving the bigger model (I).

### 5. Licensing & download availability

- **Apache-2.0**: bge-v2-m3 and v1 base/large (MIT) [1][4]; Qwen3-Reranker 0.6B/4B [5][6][7]; mxbai v1/v2 [12][13]; ms-marco MiniLM [17]; gte-modernbert [19]; granite [21]; zerank-1-small [24].
- **CC-BY-NC-4.0 (non-commercial)**: jina-v2, v3, m0 [14][15] — "For commercial usage, please refer to Jina AI's APIs" [14]. Fine for a personal NAS (I) but a blocker if Astoria is ever offered to others.
- **Checkpoints**: bge-v2-m3 ships fp32 safetensors only (2.27 GB) — no official fp16/int8/ONNX; 63 community quantizations [1]. Qwen3: BF16 only; official GGUF repos 401/404, community mradermacher GGUFs (0.6B: Q8_0 0.7 GB, f16 1.3 GB; 4B: 4.4/8.1 GB) [8]. jina-v3: "GGUF with quantizations and MLX versions are now available" — sizes unpublished [15]. mxbai: only xsmall-v1 has published ONNX (284 MB / 87.2 MB quantized) [62]; no other quant sizes [13]. MiniLM/gte/granite: standard HF downloads; FlashRank ONNX binaries for CPU [47]. bge-v2-m3 already lives in the NAS model store /mnt/ug-models (given in the task context; never delete from the store).

## Options / trade-offs

| Option | Model | Where | Quality (pack evidence) | Latency | Availability | Serving |
|---|---|---|---|---|---|---|
| A (current) | MiniLM-L6 22.7 M | NAS CPU | Modest measured gain: MRR +0.049, avoid@5 0.294→0.118 [55]; BEIR-era 74.30 TREC-DL19 [18] | 324 ms p50/30 hooks measured [55] | 24/7 | TEI (works) [34] |
| **B (recommended)** | bge-v2-m3 0.57 B | specul8 4080 (replaces existing CPU seat [63]) + NAS MiniLM fallback | MTEB-R 57.03, MLDR 59.51 [7]; BEIR 55.36 [2]; multilingual [1] | ~100–250 ms/50 (I) [49] | day (nightly-off) + NAS fallback (I) | TEI `89-1.9` [34]; already on NAS model store |
| **F (co-recommended)** | gte-multilingual-reranker-base 0.31 B | same as B | MTEB-R **59.51**, MLDR **66.33** — +2.5/+6.8 over bge, same protocol [7]; BEIR 55.4 own protocol [22]; 70+ languages [20] | same or slightly better than bge (306 M (I)) | same as B | TEI — explicitly listed in its example table [34]; one HF pull |
| C | Qwen3-Reranker-0.6B | specul8 4080 | Best ≤1.5 GB: MTEB-R 65.80, CMTEB-R 71.31, MTEB-Code 73.42 [5][7] | unpublished (Gaps); 4B is >1 s on H100 [50] | day only | vLLM (concurrency caveat [41]) or llama.cpp [44] — not TEI [35] |
| D | jina-v3 0.6 B | specul8 | Top vendor BEIR 61.94 [15][16] | 188 ms/100 docs on H100 [50] | day only | No TEI (PR abandoned [37]); no documented vLLM/llama.cpp path in pack (Gaps); NC licence [15] |
| E | mxbai-base-v2 0.5 B | specul8 or NAS | BEIR 55.57 [10]; 8 K context [13] | 0.67 s A100 (NFCorpus) [13] | — | Not TEI-arch (I) [10][34]; vLLM override [39] |

**Recommendation (inference, grounded in the facts above): Options B and F are co-equal** — bge-reranker-v2-m3 or gte-multilingual-reranker-base on the 4080 via TEI (replacing the existing specul8 CPU seat [63]), with the NAS MiniLM as fallback via the `ASTORIA_RERANK_URLS` priority list. Both clear every gate (budget, licence, TEI compatibility, multilingual); the only real differences are provenance and a small quality gap: bge is already on the NAS model store, gte is +2.5 MTEB-R / +6.8 MLDR on the same protocol [7], lighter (306 M), and explicitly listed in TEI's example table [34]. If the one-time HF download is acceptable, gte is the better default on paper (I); bge is the zero-download drop-in. If Rick wants maximum quality and accepts a non-TEI runtime, **Option C (Qwen3-0.6B via llama.cpp on the 4080)** remains the upgrade path — llama.cpp support is merged [44], its 0.7–1.3 GB GGUFs coexist better with the 13.8 GB judge (I), and it adds instruction-aware scoring ("+1% to 5%" [5]) plus 32 K context for longer episode texts (I). Re-evaluate if TEI merges PR #886 [36].

## Gaps

- No published per-query latency for Qwen3-Reranker-0.6B anywhere in the pack; only 4B ">1 s on H100" [50].
- No NAS-class CPU latency (Pentium Gold 8505/N100); closest are i7-13700K [45] and Ryzen 7 7800X3D [49]. No RTX 4080 numbers (4090 estimate only [49]).
- No official fp16/int8/ONNX checkpoints for bge-v2-m3 or Qwen3-Reranker — community quantizations only [1][8]; zerank-1-small weight size and mxbai per-model quant sizes absent.
- bge-v2-m3's BEIR/MIRACL/llama-index score tables exist only as PNGs; numbers are OCR'd and one row is illegible [2].
- No with-vs-without-reranker A/B on any memory corpus (LoCoMo/LongMemEval) — memory-system reports compare rerankers or backends, never rerank-on vs rerank-off [26][27][29].
- TEI: issue #532 (mxbai) closed "completed" but the closing comment didn't render [38]; jina-v3 serving path (vLLM/llama.cpp) undocumented in pack [37].
- Live MTEB/MMTEB leaderboards are JS-only; per-dataset MTEB-reranking values not captured. mxbai-base-v2's extended benchmark columns (multilingual/chinese/code-search) have no stated metric [10].
- The existing specul8 CPU rerank seat's served model is not explicitly stated in the pack (MiniLM assumed (I) from the compose [56]); the pack only confirms it is a TEI reranker seat, CPU (`--cpus 8`), preferred over the NAS one [55][63].
- Vendor BEIR tables disagree across vendors (different first-stage retrievers) and cannot be merged [13][15].

## Sources

[1] BAAI/bge-reranker-v2-m3 — Hugging Face model card — https://huggingface.co/BAAI/bge-reranker-v2-m3 (accessed 2026-09-23)
[2] BAAI/bge-reranker-v2-m3 — README.md (raw) — https://huggingface.co/BAAI/bge-reranker-v2-m3/raw/main/README.md (accessed 2026-09-23)
[3] FlagOpen/FlagEmbedding — README — https://github.com/FlagOpen/FlagEmbedding (accessed 2026-09-23)
[4] BAAI/bge-reranker-base — Hugging Face model card — https://huggingface.co/BAAI/bge-reranker-base (accessed 2026-09-23)
[5] Qwen/Qwen3-Reranker-0.6B — Hugging Face model card — https://huggingface.co/Qwen/Qwen3-Reranker-0.6B (accessed 2026-09-23)
[6] Qwen/Qwen3-Reranker-4B — Hugging Face model card — https://huggingface.co/Qwen/Qwen3-Reranker-4B (accessed 2026-09-23)
[7] Qwen3 Embedding: Advancing Text Embedding and Reranking Through Foundation Models (arXiv:2506.05176) — https://arxiv.org/html/2506.05176 (accessed 2026-09-23)
[8] mradermacher/Qwen3-Reranker-0.6B-GGUF — https://huggingface.co/mradermacher/Qwen3-Reranker-0.6B-GGUF (accessed 2026-09-23)
[9] mixedbread-ai/mxbai-rerank-large-v1 — Hugging Face model card — https://huggingface.co/mixedbread-ai/mxbai-rerank-large-v1 (accessed 2026-09-23)
[10] mixedbread-ai/mxbai-rerank-base-v2 — Hugging Face model card — https://huggingface.co/mixedbread-ai/mxbai-rerank-base-v2 (accessed 2026-09-23)
[11] mixedbread-ai/mxbai-rerank-large-v2 — Hugging Face model card — https://huggingface.co/mixedbread-ai/mxbai-rerank-large-v2 (accessed 2026-09-23)
[12] Boost Your Search With The Crispy Mixedbread Rerank Models (blog, 2024-02-29) — https://www.mixedbread.ai/blog/mxbai-rerank-v1 (accessed 2026-09-23)
[13] Baked-in Brilliance: Reranking Meets RL with mxbai-rerank-v2 (blog, 2025) — https://www.mixedbread.ai/blog/mxbai-rerank-v2 (accessed 2026-09-23)
[14] jinaai/jina-reranker-v2-base-multilingual — Hugging Face model card — https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual (accessed 2026-09-23)
[15] jinaai/jina-reranker-v3 — Hugging Face model card — https://huggingface.co/jinaai/jina-reranker-v3 (accessed 2026-09-23)
[16] jina-reranker-v3: Last but Not Late Interaction for Listwise Document Reranking (arXiv:2509.25085, full text) — https://arxiv.org/html/2509.25085v4 (accessed 2026-09-23)
[17] cross-encoder/ms-marco-MiniLM-L-6-v2 — Hugging Face model card — https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2 (accessed 2026-09-16)
[18] Pretrained Models — Cross Encoders (Sentence-Transformers docs) — https://www.sbert.net/docs/cross_encoder/pretrained_models.html (accessed 2026-09-16)
[19] Alibaba-NLP/gte-reranker-modernbert-base — Hugging Face model card — https://huggingface.co/Alibaba-NLP/gte-reranker-modernbert-base (accessed 2026-09-16)
[20] Alibaba-NLP/gte-multilingual-reranker-base — Hugging Face model card — https://huggingface.co/Alibaba-NLP/gte-multilingual-reranker-base (accessed 2026-09-16)
[21] ibm-granite/granite-embedding-reranker-english-r2 — Hugging Face model card — https://huggingface.co/ibm-granite/granite-embedding-reranker-english-r2 (accessed 2026-09-16)
[22] mGTE: Generalized Long-Context Text Representation and Reranking Models (arXiv:2407.19669, HTML) — https://arxiv.org/html/2407.19669 (accessed 2026-09-16)
[23] Agentset — Best Rerankers for RAG | Leaderboard — https://agentset.ai/rerankers (accessed 2026-09-23)
[24] zeroentropy/zerank-1-small — Hugging Face model card — https://huggingface.co/zeroentropy/zerank-1-small (accessed 2026-09-23)
[25] RankArena: A Unified Platform for Evaluating Retrieval, Reranking and RAG (arXiv:2508.05512) — https://arxiv.org/html/2508.05512v1 (accessed 2026-09-23)
[26] Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv:2501.13956) — https://arxiv.org/html/2501.13956v1 (accessed 2026-09-23)
[27] Cognis: Context-Aware Memory for Conversational AI Agents (arXiv:2604.19771) — https://arxiv.org/html/2604.19771 (accessed 2026-09-23)
[28] State of AI Agent Memory 2026: Benchmarks & Trends (Mem0 blog) — https://mem0.ai/blog/state-of-ai-agent-memory-2026 (accessed 2026-09-23)
[29] LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory (arXiv:2410.10813) — https://arxiv.org/html/2410.10813 (accessed 2026-09-23)
[30] Generative Agents: Interactive Simulacra of Human Behavior (arXiv:2304.03442) — https://arxiv.org/html/2304.03442v2 (accessed 2026-09-23)
[31] MemX: A Local-First Long-Term Memory System for AI Assistants (arXiv:2603.16171) — https://arxiv.org/html/2603.16171v1 (accessed 2026-09-23)
[32] Reciprocal rank fusion — Elasticsearch Reference — https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html (accessed 2026-09-23)
[33] From BM25 to Corrective RAG: Benchmarking Retrieval Strategies for Text-and-Table Documents (arXiv:2604.01733) — https://arxiv.org/html/2604.01733v1 (accessed 2026-09-23)
[34] Text Embeddings Inference README (main) — https://raw.githubusercontent.com/huggingface/text-embeddings-inference/main/README.md (accessed 2026-09-23)
[35] TEI GitHub issue search: qwen3-reranker — https://github.com/huggingface/text-embeddings-inference/issues?q=qwen3-reranker (accessed 2026-09-23)
[36] TEI PR #886: feat(qwen3): support reranker on candle backend — https://github.com/huggingface/text-embeddings-inference/issues/886 (accessed 2026-09-23)
[37] TEI PR #737: Feat/support jina v3 reranker — https://github.com/huggingface/text-embeddings-inference/issues/737 (accessed 2026-09-23)
[38] TEI issue #532: Support for mixedbread-ai/mxbai-rerank-large-v2 — https://github.com/huggingface/text-embeddings-inference/issues/532 (accessed 2026-09-23)
[39] vLLM scoring (score/rerank) docs — https://raw.githubusercontent.com/vllm-project/vllm/main/docs/models/pooling_models/scoring.md (accessed 2026-09-23)
[40] vLLM vllm/config/cache.py (gpu_memory_utilization) — https://raw.githubusercontent.com/vllm-project/vllm/main/vllm/config/cache.py (accessed 2026-09-23)
[41] vLLM issue #22048: qwen3-reranker service performance poor — https://github.com/vllm-project/vllm/issues/22048 (accessed 2026-09-23)
[42] llama.cpp tools/server/README.md — https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md (accessed 2026-09-23)
[43] llama.cpp PR #9510: llama : add reranking support — https://api.github.com/repos/ggml-org/llama.cpp/pulls/9510 (accessed 2026-09-23)
[44] llama.cpp PR #15824: Add support for Qwen3-Reranker — https://api.github.com/repos/ggml-org/llama.cpp/pulls/15824 (accessed 2026-09-23)
[45] Speeding up Inference — Cross Encoder, sentence-transformers docs — https://sbert.net/docs/cross_encoder/usage/efficiency.html (accessed 2026-09-23)
[46] ONNX Runtime: Quantize ONNX models — https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html (accessed 2026-09-23)
[47] FlashRank (PrithivirajDamodaran/FlashRank) — https://github.com/PrithivirajDamodaran/FlashRank (accessed 2026-09-23)
[48] UGREEN NASync DXP4800 Plus (4-Bay NAS) — https://ai-uk.ugreen.com/products/ugreen-nasync-dxp4800-plus-4-bay-nas (accessed 2026-09-23)
[49] Reranking & Cross-Encoders for RAG: BGE, Cohere, Jina (localaimaster, 2026) — https://localaimaster.com/blog/reranking-cross-encoders-guide (accessed 2026-09-23)
[50] Reranker Benchmark: Top 8 Models Compared (aimultiple, updated 2026-02-26) — https://aimultiple.com/rerankers (accessed 2026-09-23)
[51] Reranking: Cross-Encoders and Cascades (Jatin Bansal) — https://jatinbansal.com/ai-engineering/reranking/ (accessed 2026-09-23)
[52] Astoria docs/CONFIGURATION.md §4 Reranker (ASTORIA_RERANK_URLS) — /home/rick/repos/astoria/docs/CONFIGURATION.md:88 (accessed 2026-09-23)
[53] Astoria docs/OPERATIONS.md (reranker ops row) — /home/rick/repos/astoria/docs/OPERATIONS.md:208 (accessed 2026-09-23)
[54] Astoria docs/ARCHITECTURE.md (read path, rerank stage) — /home/rick/repos/astoria/docs/ARCHITECTURE.md:317 (accessed 2026-09-23)
[55] Astoria docs/PERFORMANCE.md §6 Reranker evaluation — /home/rick/repos/astoria/docs/PERFORMANCE.md:773 (accessed 2026-09-23)
[56] Astoria deploy/nas/docker-compose.yml (astoria-rerank service) — /home/rick/repos/astoria/deploy/nas/docker-compose.yml:49 (accessed 2026-09-23)
[57] Astoria astoria/core/rerank.py (degrade-don't-fail, cooldowns) — /home/rick/repos/astoria/astoria/core/rerank.py:10 (accessed 2026-09-23)
[58] Astoria astoria/retrieval/recall.py (rerank blend) — /home/rick/repos/astoria/astoria/retrieval/recall.py:312 (accessed 2026-09-23)
[59] Infrastructure AGENTS.md (NAS: always-on, Astoria :8933, TEI :8931) — /home/rick/projects/infrastructure/AGENTS.md:14 (accessed 2026-09-23)
[60] Infrastructure workstation.md (specul8, RTX 4080, powers off nightly) — /home/rick/projects/infrastructure/workstation.md:9 (accessed 2026-09-23)
[61] Evaluating Very Long-Term Conversational Memory of LLM Agents (LoCoMo) — https://arxiv.org/html/2402.17753 (accessed 2026-09-23)
[62] mixedbread-ai/mxbai-rerank-xsmall-v1 onnx file tree — https://huggingface.co/mixedbread-ai/mxbai-rerank-xsmall-v1/tree/main/onnx (accessed 2026-09-23)
[63] Infrastructure astoria/ops/rerank-workstation.sh (existing workstation TEI reranker seat, `--cpus 8`) — /home/rick/projects/infrastructure/astoria/ops/rerank-workstation.sh:1 (accessed 2026-09-23)
