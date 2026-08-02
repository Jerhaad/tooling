---
name: llama-swap-setup
description: "Deploy llama-swap (on top of llama.cpp) across one or more machines. Covers model selection by VRAM, multi-GPU tensor splitting, systemd service setup, and swap pool configuration."
version: 1.0.0
author: Hermes Agent
license: MIT
---

# Llama-Swap Setup

Deploy llama-swap across machines for multi-model serving with on-demand swapping. Built on llama.cpp for inference, llama-swap manages model lifecycles via a v1-compatible OpenAI endpoint.

## Installation on Linux

### 1. Install llama.cpp with CUDA

```bash
cd /opt
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j$(nproc)
```

For MTP (Multi-Token Prediction) model support, use a build after May 2026 (commit b9180+). The flag changed from `--spec-type mtp` to `--spec-type draft-mtp`.

### 2. Install a swap manager

Options:
- **llama-swap** (Python-based, recommended) — manages model download, swap, and serving
- **Custom systemd services** — one service per model, switch by killing/restarting
- **llama-api** — Python API server with model-switching support

### 3. Disable competing servers

If Ollama was previously running:
```bash
sudo systemctl stop ollama && sudo systemctl disable ollama
sudo apt remove --purge ollama
rm -rf ~/.ollama/models
```

## Model Selection by VRAM

See `references/model-zoo.md` for the current GGUF size reference table.

**Quick guide:**
- **< 16 GB VRAM:** 12B at Q8_K_XL, or 7-8B at Q5_K_M
- **16-24 GB VRAM:** 12B at Q8, 26B MoE at Q4_K_M (~18 GB), or 27B at Q8 (~18 GB)
- **24-32 GB VRAM:** 27B at Q8 (~18 GB), 26B MoE at Q5_K_M (~20 GB), or 31B at Q4_K_M (~19 GB)
- **32-48 GB VRAM (dual GPU):** 31B at Q5_K_M, or 70B-class at Q4_K_M
- **48-64 GB VRAM (triple GPU):** 70B at Q4_K_M + smaller model on secondary GPU

### Multi-GPU Tensor Split

For `llama-server` with multiple GPUs:
```bash
--tensor-split 1,1,0   # Two GPUs at full split, third unused
--tensor-split 0,0,1   # Only third GPU (e.g., 5060 Ti for fast model)
```

## Systemd Service Template

See `templates/llama-swap-service.conf` for a ready-to-configure systemd unit.

## Swap Pool Strategy

Recommended pool sizes per machine type:

**Small (1 GPU, < 32 GB):** 2-3 models spanning fast→heavy
**Medium (1 GPU, 32-48 GB):** 3-4 models
**Large (multi-GPU, 48+ GB):** 4-5 models

Pool composition principle:
- 1 **fast** model (12B or smaller) for routine tasks
- 1 **middle** model (26B-31B) for balanced reasoning
- 1 **heavy** model (27B+ dense or 35B MoE) for deep tasks
- 1 **specialist** (coding, reasoning-distilled) if applicable

## Pitfalls

* **Tokenizer hash patch:** Qwen3.6 models require a tokenizer hash patch for llama.cpp conversion (as of April 2026). Tokenizer hash: `1444df51289cfa8063b96f0e62b1125440111bc79a52003ea14b6eac7016fd5f`. If you see `NotImplementedError: BPE pre-tokenizer was not recognized`, update llama.cpp.
* **MTP flag name change:** llama.cpp renamed `--spec-type mtp` to `--spec-type draft-mtp` on May 13, 2026. Verify your build date.
* **Dynamic quantization (UD):** Unsloth Dynamic GGUFs use per-layer quantization — the actual VRAM usage may differ from the file size. A UD-Q8_K_XL file may use less VRAM than a uniform Q8_K_XL.
* **Unified memory (Mac):** Budget 8-12 GB above model size for the OS. A 26B model at Q4 that says "18 GB" needs ~26 GB of actual memory headroom on macOS.
* **No dedicated swap API:** llama-swap triggers model loads by sending a request to `/v1/chat/completions` with the target `model` field. There is no `/models/swap` endpoint.