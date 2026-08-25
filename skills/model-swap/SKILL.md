---
name: model-swap
description: "Loads a model on an on-demand inference host with a throwaway request, so the first real request does not pay for the load. Use before a heavy task."
version: 1.0.0
license: MIT
---

# Model swap

The endpoint is `$MODEL_SWAP_URL`, or `$GRADER_URL` when that is unset; both
live in `~/.config/agent-tools/env`.

A loader keeping one model resident evicts whatever else was there, so warm the
model you are about to use, not every model you might.

## Available models

Ask the endpoint; any list written here goes stale when the host is
reconfigured:

    curl -s "${MODEL_SWAP_URL:-$GRADER_URL}" | sed 's|/v1/.*|/v1/models|' | xargs curl -s | jq -r '.data[].id'

## Procedure

To swap a model, use the `execute_code` tool to send a "warm-up" request. 

### Warm-up Script
Use the following Python logic to trigger the swap. This script sends a minimal request to the endpoint and waits for a successful response.

```python
from hermes_tools import terminal
import os
import requests
import time

def swap_model(model_id: str):
    url = os.environ.get("MODEL_SWAP_URL") or os.environ["GRADER_URL"]
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}]
    }
    
    print(f"Triggering swap to {model_id}...")
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
# swap_model("qwen36-27b-mtp")
```

## Pitfalls
* **Timeout:** Loading large models (like `qwen36-27b-mtp`) can take several minutes. Ensure the request timeout is set high (600s+).
* **Verification:** Do not assume the model is ready until the request returns a 200 OK. If it fails, retry once before proceeding.
* **Network:** Ensure the agent has network access to the host the endpoint points at.
* **Host Key Verification:** If the swap fails due to "Host key verification failed", ensure the host key is added to the agent's known_hosts via `ssh-keyscan`.
* **Session Isolation:** When running in a cron job or a non-interactive session, the agent will not see the loader's success message unless the request explicitly returns a 200 OK. Ensure the `swap_model` function in the skill script returns a boolean that the calling agent can check before proceeding.
* **Llama-Swap Behavior:** The `llama-swap` mechanism loads the model on-demand based on the `model` parameter in the request. The "swap" is triggered by a standard request to the `/v1/chat/completions` endpoint; no dedicated swap command exists.
