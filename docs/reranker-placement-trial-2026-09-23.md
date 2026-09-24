# Reranker placement trial — 2026-09-23

Follow-up measurements to `research-reranker.md` / `research-reranker.local-coder.md` (both Ben-approved). Question from
Rick: run the reranker on the Threadripper CPU for now? And: the Arc Pro B50 is connected — try Qwen3-Reranker there first?

## Method

Same payload everywhere: one query, N candidate texts, 5 runs, p50. Two text lengths: ~200-token documents and Astoria's
real hook size (240 chars ≈ 60 tokens). Astoria's rerank call is top-30 facts + 6 episodes with a 3 s timeout.
Test containers only; nothing was wired into Astoria. Host load average was ~10 (vLLM seats' runtime threads) throughout.

| Where | Model / stack | 30 × ~200 tok | 30 × 240-char hook |
|---|---|---|---|
| specul8 CPU, 8 cores (live seat) | MiniLM-L6 cross-encoder, TEI ONNX | 562 ms | — (116 ms p50 measured earlier under no load) |
| specul8 CPU, 8 cores | bge-reranker-v2-m3, TEI (candle fp32, no ONNX export on the NAS) | 46.8 s | — |
| specul8 CPU, 8 cores | Qwen3-Reranker-0.6B Q8_0, llama.cpp | 39.2 s | 18.0 s |
| **Arc Pro B50**, llama.cpp SYCL | Qwen3-Reranker-0.6B f16 (8 slots) | **2.33 s** | **1.03 s** |
| Arc Pro B50, llama.cpp SYCL | Qwen3-Reranker-0.6B Q8_0 | 2.45 s | 1.14 s |
| Arc Pro B50, llama.cpp Vulkan | Qwen3-Reranker-0.6B f16 | device lost (needs the Mesa 26 container) | — |

B50 scaling is strictly linear (~75 ms per ~200-token document: 1→75 ms, 2→155, 4→295, 8→569, 16→1153); flash attention
and Q8_0 change nothing. The SYCL backend is processing rerank documents one at a time, not as a batch.

## Getting Qwen3-Reranker to score correctly in llama.cpp

Community GGUFs (`prithivMLmods/…-seq-cls-GGUF`, `Mungert/…`) load and answer, but every score is 0.0: they carry no
classifier head. Upstream `convert_hf_to_gguf.py` (2026-09-24 main, `conversion/qwen.py`) recognises `Qwen/Qwen3-Reranker-*`
by name/README and writes rank pooling, a 2-row `cls.output.weight` built from the `yes`/`no` rows of `lm_head`, and a
`rerank` chat template (the model card's system/Instruct/Query/Document prompt). Built from the original checkpoint:

    convert_hf_to_gguf.py /mnt/data/models/Qwen/Qwen3-Reranker-0.6B --outtype f16 --outfile …/Qwen3-Reranker-0.6B-rerank.f16.gguf
    llama-server -m …rerank.f16.gguf --reranking -ngl 99 -np 8 -c 16384    # POST /v1/rerank {query, documents}

Sanity: "capital of France" → Paris sentences 0.999 / 0.995, Berlin 0.0007, unrelated 0.0000. Files: `/mnt/data/models/Qwen/`
(f16 + q8_0 GGUF, HF checkpoint) mirrored to `/mnt/ug-models/Qwen/`. `tomaarsen/Qwen3-Reranker-0.6B-seq-cls` is NOT
convertible (converter rejects `Qwen3ForSequenceClassification`).

## Reading

- **CPU is out** for anything bigger than MiniLM: bge-v2-m3 and Qwen3-0.6B are 40 s per call on 8 Threadripper cores
  (Zen 2, AVX2, no VNNI). A CPU step-up from MiniLM-L6 would be MiniLM-L12 or bge-reranker-base at most.
- **B50 + Qwen3-Reranker-0.6B is usable, not fast**: ~1.2 s for Astoria's 36-hook call, inside the 3 s timeout, ~10× the
  CPU MiniLM cost. Quality is the reason to do it (MTEB-R 65.8 vs 57.0 for bge-v2-m3, the brief's numbers).
- Wiring needs a small Astoria client adapter: llama.cpp speaks Jina-style `/v1/rerank` (`documents`, `results[].relevance_score`),
  TEI speaks `/rerank` (`texts`, `[{index, score}]`). Same nightly-off fallback story as the 4080 (NAS MiniLM stays first-hit
  when specul8 is down).
- The batching ceiling on SYCL is worth one more look before committing (a Mesa 26 Vulkan container, or batching several
  documents per request on the client side).
