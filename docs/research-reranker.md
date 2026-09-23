# Research brief: which cross-encoder reranker should Astoria use, and where should it run?

*2026-09-23. Built from 11 evidence collections. `(I)` marks my inference, and everything without it is cited. This brief runs past 2,000 words because the question has five enumerated parts plus a recommendation.*

## Bottom line

- **Model: `BAAI/bge-reranker-v2-m3`, served by TEI on specul8's RTX 4080, listed first in `ASTORIA_RERANK_URLS`.** Keep the existing NAS `ms-marco-MiniLM-L-6-v2` (CPU) as the always-on fallback. bge-v2-m3 is the only candidate that meets all four conditions at once:
  - TEI's `/rerank` route supports it (XLM-RoBERTa sequence classification) [37].
  - Astoria's `/info` check accepts it (`MODEL_HINTS` includes `bge`) [6].
  - It is Apache-2.0 [12].
  - It is already on the NAS model store (per the task statement).
  - Weights are 568 M params, about 1.1 GB at fp16 [12] (I: that fits the ≤1.5 GB envelope with a small batch).
- **Do not put bge-v2-m3 (or any ≥150 M model) on the NAS CPU.** MiniLM-L6 already costs 324 ms p50 for 30 hooks there [4]. Scaling by the published V100 throughput ratios [25] puts a 568 M XLM-R-large model at roughly 15–20× that, about 5–6 s (I). That is over the 3 s read-path timeout [1], so every call would fail and fall back to base order.
- **The best quality per GB is Qwen3-Reranker-0.6B**: MTEB-R 65.80 vs 57.03 for bge-v2-m3 [15]. But it is not a drop-in:
  - TEI support is still open, unmerged PRs [38][39].
  - It needs llama.cpp or vLLM [42][43][41], which speak Jina/Cohere-style `/rerank` rather than TEI's.
  - That means code changes in `core/rerank.py` (I).
  - Treat it as the phase-2 upgrade, measured with `scripts/bench/rerank_eval.py`.
- **VRAM is the real blocker on specul8.** 3 GB sidecar + 13.8 GB judge = 16.8 GB, which is more than the card's 16 GB (I, from figures in the task statement). Before deploying, check `nvidia-smi` with both loaded. If there is no ~1.5 GB of headroom, the GPU seat is not viable and the recommendation collapses to "keep MiniLM-L6 on NAS + specul8 CPU" (the status quo) [4][10].
- **Expected trade (I):**
  - Quality: a larger first-relevant-item/precision gain than MiniLM's measured MRR 0.765→0.814 [4]. Published gains over a strong bi-encoder are small (+1.05 nDCG@10 on BEIR [13]), so re-run the 17-case eval before trusting it.
  - Latency: while specul8 is on, rerank cost should drop from ~116 ms (specul8 CPU MiniLM [4]) to tens of ms. Overnight it falls back to the NAS's ~0.6 s cold recall [4].

## Context

- **Recall pipeline today:**
  `query ─► embed (query prefix) ─► candidates ─► RRF ─► weight ─► graph expansion ─► rerank ─► collapse ─► budget ─► context` [3].
  - Reranked inputs are the top-`ASTORIA_RERANK_TOP_N` (30) facts plus the top 6 episodes [1][7].
  - Texts are capped at 240 chars (`MAX_TEXT_CHARS = 240`) [6], which is about 50–60 tokens (I). So the "~200-token" scenario in the question overstates Astoria's per-pair cost about 3–4×.
- **The contract (`docs/CONFIGURATION.md` §4, `core/rerank.py`)** [1][6]:
  - `ASTORIA_RERANK_URLS` is a priority list `url|model,url|model`. The model name is informational.
  - Each endpoint is verified through `GET /info`: TEI must report `model_type.reranker` or a model id containing `rerank`, `minilm` or `bge`.
  - Blend: `final = (1-w)·norm(base) + w·norm(sigmoid(logit))`, with w = 0.6.
  - Timeout 3.0 s. Failure cooldown 60 s; wrong-model cooldown 600 s.
  - 4096-entry `(query, text) → logit` cache.
  - If every logit sits within 1.0 of the others, base order is kept ("no opinion").
  - `rerank()` never raises: endpoint down → base order and `health.rerank="down"`.
- **Measured MiniLM-L6 result on the NAS CPU** (17 cases × 2 repetitions) [4]:

  | metric | off | on |
  |---|---|---|
  | MRR | 0.765 | **0.814** |
  | hit@k | 0.941 | 0.941 |
  | precision@5 | 0.494 | 0.459 |
  | avoid@5 (lower is better) | 0.294 | **0.118** |
  | cold p50 / p95 (ms) | 89 / 471 | 585 / 739 |

  Verdict in the doc: "a real but modest ranking gain for a few hundred milliseconds on a CPU reranker" [4].
- **Seats today:**
  - A NAS `astoria-rerank` (1 GiB, MiniLM-L6, CPU, TEI on `:8935` [2]) and a second reranker on the workstation, marked "nightly-off"; "the NAS copies are the fallback" [4].
  - The workstation seat is **CPU** (`--cpus 8`) [10]: 116 ms p50 per 30-hook call vs 324 ms on the NAS [4].
  - No GPU reranker seat exists yet.
- **Availability:**
  - The NAS is "Always-on" and hosts Astoria `:8933` and TEI `:8931` [8].
  - specul8 "Powers off nightly" [8], manually [9].
  - Astoria "must **survive the workstation's nightly power-off**" (infra requirements).
- **Memory / prior decisions:** memory records the reranker choice as an open question: "research brief docs/research-reranker.md deciding which cross-encoder reranker Astoria should use as a second recall stage and where it should run" [11]. The repo docs already record the direction: move the stage to a GPU endpoint to raise `top_n` [4].

## Findings

### 1. Candidates: size, context, languages, licence

| model | params | weights on disk | max seq | languages | licence | TEI `/rerank`? |
|---|---|---|---|---|---|---|
| ms-marco-MiniLM-L-6-v2 (current) | 22.7 M [26] | ~90 MB fp32 (I) | 512 [25] | English | Apache-2.0 [26] | yes (BERT; deployed) [5] |
| ms-marco-MiniLM-L-12-v2 | 33.4 M [29] | — | 512 [29] | English | Apache-2.0 | yes (BERT) (I) |
| bge-reranker-v2-m3 | 567,755,777 [12] | 2.27 GB fp32 only (no official fp16/int8/ONNX) [12] | 8194 positions [14]; card example truncates at 512; evals run at 1024 [13] | multilingual (bge-m3 backbone) [13] | Apache-2.0 [12] | yes (XLM-RoBERTa seq-cls) [37][41] |
| Qwen3-Reranker-0.6B | 595,776,512, BF16 1.19 GB [15] | GGUF Q8_0 0.7 GB, f16 1.3 GB (community) [18] | 32k [15] | 100+ [15] | Apache-2.0 [15] | **no**: open PRs only [38][39] |
| Qwen3-Reranker-4B | 4.02 B, 8.04 GB BF16 [16] | GGUF Q8_0 4.4 GB (community) | 32k [16] | 100+ | Apache-2.0 | no |
| mxbai-rerank-xsmall/base/large-v1 | 70.8 M / 184 M / 435 M [21] | fp16 135 MiB / 352 MiB / 830 MiB; xsmall int8 ONNX 87.2 MB [21] | 512 [20] | English | Apache-2.0 [20] | yes, probably (DeBERTa? unverified) — see Gaps |
| mxbai-rerank-base-v2 | 494 M (Qwen-2.5-based) [19] | fp16 ~943 MiB [19] | 8K (32K-compatible) [20] | 100+ [20] | Apache-2.0 [20] | unclear (see Disagreements) |
| mxbai-rerank-large-v2 | 1.54 B [20] | fp16 2.88 GiB | 8K [20] | 100+ | Apache-2.0 | unclear; over 1.5 GB anyway |
| jina-reranker-v2-base-multilingual | 278 M, BF16 557 MB [22] | — | 1024 [22] | 100+ | **CC-BY-NC-4.0** [22] | not verified |
| jina-reranker-v3 (listwise, Qwen3-0.6B) | 596.8 M, BF16 1.19 GB [23] | GGUF/MLX exist [23] | 131K, ≤64 docs per pass [23] | multilingual | **CC-BY-NC-4.0** [23] | **no**: request open, PR abandoned [51] |
| gte-reranker-modernbert-base | 149 M [27] | — | 8192 [27] | English | Apache-2.0 [27] | yes (ModernBERT) [37] |
| gte-multilingual-reranker-base | 306 M [28] | — | 8192 [28] | 70+ [28] | Apache-2.0 (I) | yes (GTE) [37] |
| granite-embedding-reranker-english-r2 | 149 M (ModernBERT) [30] | — | 8192 [30] | English | Apache-2.0 [30] | not verified |
| zerank-1-small (2025 leader-class) | 1.72 B [31] | >1.5 GB | — | — | Apache-2.0 [31] | no evidence |

- Qwen3-Reranker scores through a causal LM: "By default, scores are raw logit differences" [15]. It is instruction-sensitive, with 1–5 % loss without an instruct [15]. (I) Astoria's `sigmoid(logit)` blend would still work, but the "within 1.0 = no opinion" rule was tuned on MiniLM logits and needs re-checking for any new model.

### 2. Retrieval-quality evidence

**Gains over the bi-encoder alone (rerank top-100):**
- **BGE card, BEIR:** bge-en-v1.5-large 54.31 → bge-v2-m3 **55.36** (+1.05); v2-gemma 60.71 [13].
- **BGE card, MIRACL:** bge-m3 67.91 → v2-m3 **72.84** [13].
- These scores exist only as images on the card and were OCR'd by the collector (see Gaps).
- **Qwen3 report** (baseline Qwen3-Embedding-0.6B, MTEB-R 61.82) [17][15]:
  - Qwen3-Reranker-0.6B **65.80** (+3.98)
  - 4B **69.76**
  - bge-reranker-v2-m3 **57.03**
  - jina-v2 58.22
  - gte-multilingual 59.51
- **(I)** In Qwen's table bge-v2-m3 *lowers* the score below a strong embedder. A reranker helps most when the first stage is weak.
- **nomic-embed-v1.5's standing is unmeasured here** (Gap). Astoria's own 17-case eval is the only evidence that counts for this corpus.

**Cross-vendor comparison (mixedbread v2 blog, BM25 first stage, BEIR nDCG@10)** [20]:
- mxbai-large-v2 57.49
- mxbai-base-v2 55.57
- jina-v2 54.35
- bge-v2-m3 53.94
- mxbai-large-v1 49.32

**Small English models (IBM table, BEIR avg)** [30]:
- MiniLM-L12 52.0
- bge-reranker-base 51.6
- gte-reranker-modernbert-base 54.8 (its own card claims 56.73) [27]

**MiniLM on MS MARCO / TREC-DL** [25]:
- L6: 74.30 nDCG@10 (TREC-DL 19)
- L12: 74.31
- (I) Going from L6 to L12 buys almost nothing.

**2025–26 leaders:**
- Agentset (updated Feb 15, 2026) ranks Zerank 2 #1 at 1638 ELO [52].
- jina-v3 claims BEIR 61.94 (the paper body says 61.85) [23][24], but it is non-commercial and has no TEI support.
- Qwen3-Reranker-4B/8B lead the Qwen-run MTEB-R [17].
- None of the leaders that fit in ≤1.5 GB are both TEI-servable and Apache-licensed except via bge-v2-m3 / gte (I).

**Memory / personal-knowledge corpora:**
- **Zep** uses cross-encoders as its top reranking tier and ran "the BGE-m3 models from BAAI for both reranking and embedding tasks" [32].
- **Cognis** (LoCoMo) runs RRF → temporal boost → a BGE cross-encoder, which "adds approximately 20-50ms latency" [33]. There is no no-reranker ablation, and in its temporal-error analysis 22 % of failures were reranker ordering errors [33].
- **Candidate depth (T²-RAGBench):**
  - "With only 20 candidates, reranking is ineffective (Recall@5: 0.458)". This rises to 0.826 at 50 candidates [34].
  - Hybrid + reranker gives Recall@5 0.816 vs 0.695 for hybrid RRF alone [34].
  - (I) Astoria's hit@k 0.941 at top-30 suggests its pool is adequate, but on a GPU, top-50 is worth testing.
- **No study was found with a clean with/without-reranker A/B on LoCoMo or LongMemEval** (Gap).

### 3. Latency and serving

**Astoria-specific measurements (fact)** [4][5]:
- NAS CPU MiniLM-L6, TEI ONNX: ~11 ms/hook, ~0.3 ms/token, 323.5 ms p50 / 680.7 ms p95 per 30-hook call, ~2.9 calls/s.
- specul8 CPU seat: 116.4 ms p50, ~12 calls/s.
- The compose comments record ONNX as about 2× faster than Candle on the NAS (~11 vs ~22 ms/hook) [5].

**The NAS CPU** is a 12th-gen Intel Pentium Gold 8505, 5 cores / 6 threads [46]. No published reranker benchmark on this class of CPU exists (Gap).

**Scaling estimates (I):**
- sbert's V100 docs/s figures [25]: L6 1800, L12 960, electra-base 340. A 12-layer base model is therefore about 5× L6.
- Applying that to the NAS's 324 ms:

  | model | est. per 30-hook call (NAS CPU) |
  |---|---|
  | bge-reranker-base / gte-multilingual (12-layer base) | ~1.5–2 s |
  | gte-reranker-modernbert-base (22 layers) | ~2–3 s |
  | bge-v2-m3 (24-layer large) | ~5–6 s, over the 3 s timeout |

- For the question's scenario of 50 × 200 tokens, even MiniLM costs about 10k tokens × 0.3 ms ≈ 3 s on the NAS.

**GPU (published, not an RTX 4080):**
- H100 batched, 100 pairs of 512 tokens [47]: bge-v2-m3 90 ms, jina-v2 28 ms, mxbai-base 24 ms. "RTX 4090: ~50% of H100 throughput" [47] (an estimate).
- aimultiple (H100, 100 docs) [48]: jina-v3 188 ms; Qwen3-Reranker-4B "takes over a second per query" [48].
- sbert (RTX 3090): "Half precision still pays off 2 to 3x on the larger rerankers" [44].
- Generic figure: "5–20 ms per pair on GPU; 50–200 ms total for a top-50 rerank" for 300M–1.5B models [49].
- **(I)** For bge-v2-m3 fp16 on a 4080 with 36 pairs × ~60 tokens (≈2k tokens, versus 51k tokens in the H100 test), expect roughly 15–40 ms of compute plus LAN overhead.

**CPU int8:** ONNX Runtime int8 depends on VNNI/AVX support, and "Old hardware has none or few of the instructions needed to perform efficient inference in int8" [45]. sbert found that "ONNX and OpenVINO can even perform slightly worse than PyTorch" [44]. Treat int8 as something to test, not a plan.

**Serving options:**

| server | supports | notes |
|---|---|---|
| **TEI** [37] | XLM-RoBERTa/CamemBERT/RoBERTa/GTE seq-cls, plus ModernBERT (`gte-reranker-modernbert-base`) | Images: `cpu-1.9` (x86), `89-1.9` (Ada/RTX 40xx). Knobs: `--max-batch-tokens` (default 16384; NAS uses 4096 [5]), `--max-client-batch-size` 32. Qwen3-Reranker: unmerged PR #886, which notes the stock config lacks `id2label`, so TEI cannot resolve it end-to-end [38]. jina-v3: PR abandoned [51]. **Only TEI matches Astoria's current client contract** [1]. |
| **vLLM** [41] | bge-v2-m3 natively (`XLMRobertaForSequenceClassification`); Qwen3-Reranker via `--hf_overrides` to `Qwen3ForSequenceClassification`; mxbai-v2 via `Qwen2ForSequenceClassification` | `/rerank` / `/v1/rerank` are Jina/Cohere-compatible. Default `gpu_memory_utilization` is 0.92 (I: that would fight the llama.cpp judge unless capped hard). |
| **llama.cpp** | bge-v2-m3 [42]; Qwen3-Reranker since PR #15824 [43] | `--reranking` enables `/reranking` (aliases `/rerank`, `/v1/rerank`) [42]. (I) Rick already runs llama.cpp for the judge, so a 0.7 GB Q8_0 Qwen3-0.6B second instance is operationally cheap, but Astoria's client would need a Cohere-style adapter and a different `/info` check. |
| **sentence-transformers CrossEncoder** | in-process | Removes a network hop but moves model RAM into the Astoria container (I). |

### 4. How the recall pipeline should use it

- **Keep the design: bi-encoder/BM25 RRF → top-N → cross-encoder → blend → cut** [3][1]. This matches the two-stage pattern used in production memory systems [32][33].
- **Fusion with recency/importance:**
  - Astoria blends min-max-normalised base and reranker scores with w = 0.6 [1], so recency, importance and graph weighting survive inside `base`.
  - This matches Generative Agents, which min-max normalises recency, importance and relevance and sets all weights to 1 [35].
  - It also matches MemX, which re-ranks on similarity, recency, importance and frequency with weights 0.45 / 0.25 / 0.10 / 0.05 and a 30-day half-life [36].
  - (I) Keep a linear blend. Do not replace `base` with the raw reranker score, or recency/importance are lost.
- **Changes when moving to a GPU endpoint (I):**
  - Raise `ASTORIA_RERANK_TOP_N` to about 50 on GPU, since T²-RAGBench shows depth matters [34].
  - Keep 240 chars for facts. Consider ~512 chars for episodes, because bge-v2-m3 handles longer input.
  - Keep the 3 s timeout. Consider a shorter one (~0.5 s) for the specul8 entry so a hung GPU seat fails over quickly.
- **Contract detail — cache risk (I, unverified):** the logit cache is keyed on `(query, text)` [6]. If it is not also keyed on model, then after failover MiniLM and bge logits would mix within one reranked set. Min-max normalisation doesn't fix mixed scales, and the "no opinion" threshold is scale-specific. Check `core/rerank.py` and key the cache by the active endpoint.
- **If the reranker runs on specul8 instead of the NAS:**
  - Order the variable `ASTORIA_RERANK_URLS=http://specul8:8935|bge-reranker-v2-m3,http://nas.local:8935|ms-marco-MiniLM-L-6-v2` (I; this follows the documented `url|model` priority format [1]).
  - When specul8 powers off, the 60 s failure cooldown plus priority order means recall moves to the NAS automatically [1][6].
  - At that point quality drops back to MiniLM and cold recall is ≈ 0.6 s, with the "NAS reranker alone" capping recall at <3 req/s [4].
  - (I) Expect a quality step at night. That is acceptable only because the NAS seat keeps the stage alive.
  - The NAS must stay the fallback per the survive-the-power-off requirement [8].

### 5. Licensing and availability

| licence | models |
|---|---|
| Apache-2.0 | bge-reranker-v2-m3 [12], Qwen3-Reranker 0.6B/4B [15][16], mxbai v1/v2 [20], MiniLM cross-encoders [26], gte-reranker-modernbert-base [27], granite-r2 [30], zerank-1-small [31] |
| MIT | bge-reranker-base/large (per the BGE cards) |
| CC-BY-NC-4.0 | jina v2 [22] and jina v3 [23]. For v3: "If you need to use it beyond those platforms or on-premises within your company, note that the model is licensed under CC BY-NC 4.0." [23] (I) Personal non-commercial use is allowed; it is still the only licence friction in the set. |

**Downloads:** all are public on Hugging Face.
- Qwen's official GGUF repos returned 401; only community GGUFs (mradermacher) were reachable [18].
- bge-v2-m3 ships fp32 only, so TEI converts to fp16 at load on GPU (I).

## Options / trade-offs

| option | quality (expected) | latency / recall | ops cost | verdict |
|---|---|---|---|---|
| A. Status quo: MiniLM-L6, NAS + specul8 CPU | measured MRR +0.049, avoid@5 −0.18 [4] | 116 ms (day) / 324 ms (night) [4] | none | baseline |
| **B. bge-v2-m3 on 4080 (TEI `89-1.9`) → NAS MiniLM fallback** | multilingual, BEIR +1 over a strong embedder [13]; larger vs weaker (I) | ~15–40 ms + LAN (I); night as A | ~1.1–1.5 GB VRAM; config-only change [3] | **recommended if VRAM fits** |
| C. Qwen3-Reranker-0.6B on 4080 (llama.cpp Q8_0 0.7 GB or vLLM) | best ≤1.5 GB: MTEB-R 65.80 [15] | similar to B (I); no published 0.6B latency | client adapter for Cohere-style API + `/info` rule (I) | phase 2, after measuring B |
| D. gte-reranker-modernbert-base on NAS CPU | BEIR 54.8–56.73 [30][27], English | ~2–3 s at top-30 (I), at the timeout | TEI-supported, drop-in [37] | only with `TOP_N` ≈ 10 (I) |
| E. bge-v2-m3 on NAS CPU | as B | ~5–6 s (I) > 3 s timeout | — | reject |

## Gaps

- **VRAM headroom on the 4080:** the stated 3 + 13.8 GB already exceeds 16 GB. Real residency and headroom were not measured.
- **Latency on target hardware:** no published measurement on an RTX 4080, a Pentium Gold 8505 / N100-class CPU, or for Qwen3-Reranker-0.6B at all. All GPU and NAS estimates for non-MiniLM models are extrapolations (I).
- **BGE v2 reranker scores** exist only as PNG images on the model card. The BEIR/MIRACL numbers above were OCR'd by the collector.
- **Leaderboards unreadable:** the MTEB/MMTEB live leaderboard (a JS app) and the AIR-Bench leaderboard could not be fetched. There is no per-dataset MTEB-reranking figure for jina, and no standalone ZeroEntropy leaderboard.
- **nomic-embed-text-v1.5 as first stage:** no published reranker gain over it specifically, so gains over nomic are unknown.
- **Memory-benchmark A/B:** no clean with-vs-without-reranker ablation on LoCoMo or LongMemEval. Mem0's paper, Letta, MemGPT and MemoryBank report no reranker stage.
- **Model support details:**
  - TEI support for mxbai-v2 is contested (see Disagreements).
  - TEI support for mxbai-v1 (DeBERTa) and granite-r2 is unverified.
  - The release that added ModernBERT/GTE rerankers is not visible.
- **Quantisation:** no official fp16/int8/ONNX checkpoint for bge-v2-m3. No reranker-specific int8 CPU speedup figure. The sbert per-model ratios exist only in charts.
- **vLLM:** no documented minimum GPU memory for rerankers.
- **specul8 power-off:** the exact power-off time is not documented.
- **Astoria code:** whether the logit cache is keyed per model was not checked.

## Disagreements recorded by the collectors

- **mxbai-v2 in TEI:**
  - Collector 08 inferred that mxbai-rerank-large-v2 is an XLM-RoBERTa classifier and is therefore now supported [40].
  - Collectors 03 and 09 show v2 is Qwen-2.5-based, and vLLM serves it via `Qwen2ForSequenceClassification` [19][41].
  - Treat TEI support as **unverified**.
- **bge-v2-m3 BEIR score:**
  - 55.36 on the BGE card (bge-en first stage) [13]
  - 53.94 on mxbai's blog (BM25 first stage) [20]
  - 56.51 in jina's comparison table [23]
  - These use different protocols; do not average them.
- **jina-v2 speed:** Jina claims 15× the throughput of bge-v2-m3 [50]. The H100 table shows about 3.2× (3500 vs 1100 pairs/s) [47].
- **jina-v2 context:** 1024 on its card [22] vs 8192 in Jina's m0 blog.
- **llama.cpp flags:** `--embedding --pooling rank` vs `--reranking` is still unsettled across README and PRs [42].

## Sources

[1] Astoria docs/CONFIGURATION.md §4 Reranker — /home/rick/repos/astoria/docs/CONFIGURATION.md:63 (accessed 2026-09-23)
[2] Astoria docs/OPERATIONS.md — /home/rick/repos/astoria/docs/OPERATIONS.md:16 (accessed 2026-09-23)
[3] Astoria docs/ARCHITECTURE.md (read path, rerank) — /home/rick/repos/astoria/docs/ARCHITECTURE.md:317 (accessed 2026-09-23)
[4] Astoria docs/PERFORMANCE.md §6–§7 — /home/rick/repos/astoria/docs/PERFORMANCE.md:773 (accessed 2026-09-23)
[5] Astoria deploy/nas/docker-compose.yml (astoria-rerank) — /home/rick/repos/astoria/deploy/nas/docker-compose.yml:49 (accessed 2026-09-23)
[6] Astoria astoria/core/rerank.py — /home/rick/repos/astoria/astoria/core/rerank.py:31 (accessed 2026-09-23)
[7] Astoria astoria/retrieval/recall.py — /home/rick/repos/astoria/astoria/retrieval/recall.py:41 (accessed 2026-09-23)
[8] Infrastructure AGENTS.md (host map) — /home/rick/projects/infrastructure/AGENTS.md:12 (accessed 2026-09-23)
[9] Infrastructure workstation.md — /home/rick/projects/infrastructure/workstation.md:8 (accessed 2026-09-23)
[10] Infrastructure astoria/ops/rerank-workstation.sh — /home/rick/projects/infrastructure/astoria/ops/rerank-workstation.sh:1 (accessed 2026-09-23)
[11] Astoria memory recall ("Astoria recall rerank decisions") — /home/rick/.config/pi-jobs/142722-763a/fetch/memory-recall-astoria.md:1 (accessed 2026-09-23)
[12] BAAI/bge-reranker-v2-m3 model card — https://huggingface.co/BAAI/bge-reranker-v2-m3 (accessed 2026-09-23)
[13] BAAI/bge-reranker-v2-m3 README (raw) — https://huggingface.co/BAAI/bge-reranker-v2-m3/raw/main/README.md (accessed 2026-09-23)
[14] BAAI/bge-reranker-v2-m3 config.json — https://huggingface.co/BAAI/bge-reranker-v2-m3/raw/main/config.json (accessed 2026-09-23)
[15] Qwen/Qwen3-Reranker-0.6B model card — https://huggingface.co/Qwen/Qwen3-Reranker-0.6B (accessed 2026-09-23)
[16] Qwen/Qwen3-Reranker-4B model card — https://huggingface.co/Qwen/Qwen3-Reranker-4B (accessed 2026-09-23)
[17] Qwen3 Embedding technical report (arXiv 2506.05176) — https://arxiv.org/html/2506.05176 (accessed 2026-09-23)
[18] mradermacher/Qwen3-Reranker-0.6B-GGUF — https://huggingface.co/mradermacher/Qwen3-Reranker-0.6B-GGUF (accessed 2026-09-23)
[19] mixedbread-ai/mxbai-rerank-base-v2 model card — https://huggingface.co/mixedbread-ai/mxbai-rerank-base-v2 (accessed 2026-09-23)
[20] Mixedbread blog: mxbai-rerank-v2 — https://www.mixedbread.ai/blog/mxbai-rerank-v2 (accessed 2026-09-23)
[21] mixedbread-ai/mxbai-rerank-xsmall-v1 model card — https://huggingface.co/mixedbread-ai/mxbai-rerank-xsmall-v1 (accessed 2026-09-23)
[22] jinaai/jina-reranker-v2-base-multilingual model card — https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual (accessed 2026-09-23)
[23] jinaai/jina-reranker-v3 model card — https://huggingface.co/jinaai/jina-reranker-v3 (accessed 2026-09-23)
[24] jina-reranker-v3 paper (arXiv 2509.25085v4) — https://arxiv.org/html/2509.25085v4 (accessed 2026-09-23)
[25] Sentence-Transformers: Pretrained Cross-Encoders — https://www.sbert.net/docs/cross_encoder/pretrained_models.html (accessed 2026-09-16)
[26] cross-encoder/ms-marco-MiniLM-L-6-v2 model card — https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2 (accessed 2026-09-16)
[27] Alibaba-NLP/gte-reranker-modernbert-base model card — https://huggingface.co/Alibaba-NLP/gte-reranker-modernbert-base (accessed 2026-09-16)
[28] Alibaba-NLP/gte-multilingual-reranker-base model card — https://huggingface.co/Alibaba-NLP/gte-multilingual-reranker-base (accessed 2026-09-16)
[29] cross-encoder/ms-marco-MiniLM-L-12-v2 model card — https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-12-v2 (accessed 2026-09-16)
[30] ibm-granite/granite-embedding-reranker-english-r2 model card — https://huggingface.co/ibm-granite/granite-embedding-reranker-english-r2 (accessed 2026-09-16)
[31] zeroentropy/zerank-1-small model card — https://huggingface.co/zeroentropy/zerank-1-small (accessed 2026-09-23)
[32] Zep: A Temporal Knowledge Graph Architecture for Agent Memory — https://arxiv.org/html/2501.13956v1 (accessed 2026-09-23)
[33] Cognis: Context-Aware Memory for Conversational AI Agents — https://arxiv.org/html/2604.19771 (accessed 2026-09-23)
[34] From BM25 to Corrective RAG (T²-RAGBench) — https://arxiv.org/html/2604.01733v1 (accessed 2026-09-23)
[35] Generative Agents: Interactive Simulacra of Human Behavior — https://arxiv.org/html/2304.03442v2 (accessed 2026-09-23)
[36] MemX: A Local-First Long-Term Memory System for AI Assistants — https://arxiv.org/html/2603.16171v1 (accessed 2026-09-23)
[37] Text Embeddings Inference README — https://raw.githubusercontent.com/huggingface/text-embeddings-inference/main/README.md (accessed 2026-09-23)
[38] TEI PR #886: Qwen3 reranker on candle — https://github.com/huggingface/text-embeddings-inference/issues/886 (accessed 2026-09-23)
[39] TEI issue search: qwen3-reranker — https://github.com/huggingface/text-embeddings-inference/issues?q=qwen3-reranker (accessed 2026-09-23)
[40] TEI issue #532: mxbai-rerank-large-v2 — https://github.com/huggingface/text-embeddings-inference/issues/532 (accessed 2026-09-23)
[41] vLLM scoring (score/rerank) docs — https://raw.githubusercontent.com/vllm-project/vllm/main/docs/models/pooling_models/scoring.md (accessed 2026-09-23)
[42] llama.cpp server README — https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md (accessed 2026-09-23)
[43] llama.cpp PR #15824: Qwen3-Reranker support — https://api.github.com/repos/ggml-org/llama.cpp/pulls/15824 (accessed 2026-09-23)
[44] Sentence-Transformers: CrossEncoder speeding up inference — https://sbert.net/docs/cross_encoder/usage/efficiency.html (accessed 2026-09-23)
[45] ONNX Runtime: Quantize ONNX models — https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html (accessed 2026-09-23)
[46] UGREEN NASync DXP4800 Plus — https://ai-uk.ugreen.com/products/ugreen-nasync-dxp4800-plus-4-bay-nas (accessed 2026-09-23)
[47] Reranking & Cross-Encoders for RAG (localaimaster) — https://localaimaster.com/blog/reranking-cross-encoders-guide (accessed 2026-09-23)
[48] Reranker Benchmark: Top 8 Models Compared (aimultiple) — https://aimultiple.com/rerankers (accessed 2026-09-23)
[49] Reranking: Cross-Encoders and Cascades (Jatin Bansal) — https://jatinbansal.com/ai-engineering/reranking/ (accessed 2026-09-23)
[50] jina-reranker-v2-base-multilingual (Jina AI models page) — https://jina.ai/models/jina-reranker-v2-base-multilingual/ (accessed 2026-09-23)
[51] TEI issue search: jina-reranker — https://github.com/huggingface/text-embeddings-inference/issues?q=jina-reranker (accessed 2026-09-23)
[52] Agentset: Best Rerankers for RAG leaderboard — https://agentset.ai/rerankers (accessed 2026-09-23)
