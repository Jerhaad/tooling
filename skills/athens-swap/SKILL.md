---
name: athens-swap
description: \"Triggers a model swap on the Athens machine by sending a pre-flight request to the v1 endpoint. Use this before starting heavy tasks to ensure the requested model is loaded into memory.\"
version: 1.0.0
author: Hermes Agent
license: MIT
---

# Athens Model Swap

This skill handles the 'warm-up' logic for the Athens machine, whose
OpenAI-compatible endpoint is `$ATHENS_URL` (set in `~/.config/hermes-tools/env`). Because the `llama-swap` mechanism loads models on-demand, this skill ensures the model is active before the agent begins a task.

## Available Models
- `gemma4-12b`
- `qwen36-27b`
- `qwen36-35b`
- `qwen36-27b-mtp`

## Procedure

To swap a model, use the `execute_code` tool to send a "warm-up" request. 

### Warm-up Script
Use the following Python logic to trigger the swap. This script sends a minimal request to the endpoint and waits for a successful response.

```python
from hermes_tools import terminal
import os
import requests
import time

def swap_athens_model(model_id: str):
    url = os.environ["ATHENS_URL"]
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}]
    }
    
    print(f"Triggering swap to {model_id} on Athens...")
    try:
        response = requests.post(url, json=payload, timeout=300)
        if response.status_code == 200:
            print(f"Successfully swapped to {model_id}.")
            return True
        else:
            print(f"Swap failed with status {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"Error during swap: {e}")
        return False

# Example usage:
# swap_athens_model("qwen36-27b-mtp")
```

## Pitfalls
* **Timeout:** Loading large models (like `qwen36-27b-mtp`) can take several minutes. Ensure the request timeout is set high (600s+).
* **Verification:** Do not assume the model is ready until the request returns a 200 OK. If it fails, retry once before proceeding.
* **Network:** Ensure the agent has network access to the host `$ATHENS_URL` points at.
* **Host Key Verification:** If the swap fails due to "Host key verification failed", ensure the host key is added to the agent's known_hosts via `ssh-keyscan`.
* **Session Isolation:** When running in a cron job or a non-interactive session, the agent will not see the `llama-swap` success message unless the request explicitly returns a 200 OK. Ensure the `swap_athens_model` function in the skill script returns a boolean that the calling agent can check before proceeding.
* **Llama-Swap Behavior:** The `llama-swap` mechanism loads the model on-demand based on the `model` parameter in the request. The "swap" is triggered by a standard request to the `/v1/chat/completions` endpoint; no dedicated swap command exists.
