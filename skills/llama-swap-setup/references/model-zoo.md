# Current GGUF Model Zoo (as of July 2026)

Sources: Unsloth Dynamic 2.0, Hugging Face, official model repos. All files are GGUF format for llama.cpp.

## Gemma 4 (Google, April 2026, Apache 2.0)

| Model | Architecture | Q4_K_M | Q5_K_M | Q8_K_XL | VRAM Min | VRAM Rec |
|-------|-------------|--------|--------|---------|----------|----------|
| 12B-it | Dense, multimodal (text+img+audio) | ~8 GB | ~9 GB | 13.6 GB | 8 GB | 14 GB |
| 26B-A4B-it | MoE (26B total, 4B active) | ~18 GB | ~20 GB | ~28 GB | 18 GB | 28 GB |
| 31B-it | Dense | 18.8 GB | 21.9 GB | ~34 GB | 20 GB | 36 GB |

### Hugging Face repos (Unsloth)
- `unsloth/gemma-4-12b-it-GGUF` — omni GGUF (text+image+audio)
- `unsloth/gemma-4-26B-A4B-it-GGUF`
- `unsloth/gemma-4-31B-it-GGUF`

### Key notes
- 12B delivers quality near the 26B while running on 16 GB — good fast model
- 26B-A4B is MoE with only 4B active → fast token generation despite large param count
- 31B is dense — highest quality but tightest VRAM fit at Q4 (~19 GB + KV cache)

## Qwen3.6 (Alibaba, April 2026)

| Model | Architecture | Q4_K_M | Q4_K_XL (UD) | Q8_0 | Q8_K_XL (UD) | VRAM Min | VRAM Rec |
|-------|-------------|--------|-------------|------|-------------|----------|----------|
| 27B | Dense | ~16 GB | ~16.5 GB | ~27 GB | ~18 GB | 17 GB | 28 GB |
| 27B-MTP | Dense + speculative heads | ~16 GB | ~17 GB | ~27 GB | ~18 GB | 17 GB | 28 GB |
| 35B-A3B | MoE (35B total, 3B active) | ~42 GB | ~43 GB | ~52 GB | 38.5 GB | 5 GB (active) | 10 GB (active) |

### Hugging Face repos (Unsloth)
- `unsloth/Qwen3.6-27B-GGUF` — dense
- `unsloth/Qwen3.6-27B-MTP-GGUF` — MTP (speculative decoding)
- `unsloth/Qwen3.6-35B-A3B-GGUF` — MoE, 3B active
- `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` — MoE + MTP

### Key notes
- 27B-MTP gives 1.5-2x speedup via native speculative decoding in llama.cpp (requires build after May 2026)
- 35B-A3B: despite 42 GB file size, only ~3B params active per forward → runs at small-model speed with 35B knowledge
- **Tokenizer hash required:** `1444df51289cfa8063b96f0e62b1125440111bc79a52003ea14b6eac7016fd5f` for llama.cpp conversion
- 35B-A3B Q8_K_XL is 38.5 GB — **won't fit on 32 GB unified memory**

## Quantization Reference

| Level | Description | Quality | Typical reduction |
|-------|-------------|---------|-------------------|
| Q2_K | 2-bit | Noticeable degradation | ~85% |
| Q3_K_M | 3-bit mid | Acceptable for fast tasks | ~75% |
| Q4_K_M | 4-bit mid | **Best quality/speed balance** | ~70% |
| Q4_K_XL | 4-bit extended | Slightly better than Q4_K_M | ~70% |
| Q5_K_M | 5-bit mid | Near-lossless | ~60% |
| Q6_K | 6-bit | Virtually indistinguishable | ~50% |
| Q8_0 | 8-bit uniform | Lossless for practical use | ~40% |
| Q8_K_XL (UD) | Unsloth Dynamic 8-bit | Per-layer 8-bit | ~40% |

**Unsloth Dynamic (UD):** Per-layer intelligent quantization. More sensitive layers get higher bit-width, less sensitive get lower. Outperforms uniform quants at same average bit-width.

## Other Notable Families

| Model | Q4_K_M | VRAM | Notes |
|-------|--------|------|-------|
| Llama 3.3-70B-Instruct | ~42 GB | 44+ GB | Excellent generalist, dense |
| Mixtral-8x22B-Instruct | ~48 GB | 50+ GB | MoE, fast token gen, dense routing |
| DeepSeek-R1 (distilled) | varies | varies | Reasoning-specialized |
| Qwen3-Coder-Next | ~45 GB | 47+ GB | Code-specialist, dense |
