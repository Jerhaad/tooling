---
name: hermes-ops
description: Diagnose the hermes agent's web search and extraction, its chat platforms, and its session transcripts. Use when hermes cannot search or extract, when a chat channel is down or changing, when one profile behaves differently from the rest, or when the user asks for a hermes conversation log. Triggers - "hermes can't search", "web extract is broken", "check the hermes gateway".
---

# Hermes operations

Every fault below reports success at the layer you would probe. Run `hermes-doctor` first.

```bash
hermes-doctor all
hermes-doctor web --live                # calls web_search/web_extract for real
hermes-doctor web --fix                 # pin backends where extract resolves search-only
hermes-doctor platforms
hermes-doctor sessions --source telegram
hermes-doctor sessions --export <id>
```

Exit `0` clean, `1` fault found, `2` a check could not run.

## Profiles are the unit of drift

`~/.hermes/profiles/<name>/` is a complete home whose unset values fall back to built-in defaults,
not to root. One lane breaks while the rest work, so check every profile after any change.

Select one with `HERMES_HOME=~/.hermes/profiles/<name>`. `HERMES_PROFILE` is silently ignored and
returns root's values.

## Web search and extraction

Search is SearXNG (`SEARXNG_URL`); extraction is a separate provider.

Set `backend`, `search_backend` and `extract_backend` together. A blank `extract_backend`
autodetects to the search backend, which cannot extract, and filling one while leaving the rest
blank re-autodetects those. Valid extract backends: `firecrawl, tavily, keenable, exa, parallel`.

`parallel` needs no `PARALLEL_API_KEY`: `keyless_mcp.use_keyless()` routes it through a free MCP
tier.

### When search returns zero

Read `unresponsive_engines`, which the agent never surfaces:

```bash
SEARXNG_URL=$(grep -E '^SEARXNG_URL=' ~/.hermes/.env | cut -d= -f2-)   # not in the shell
curl -s "$SEARXNG_URL/search?q=test&format=json" | python3 -m json.tool | head -40
```

Every engine timing out at once is one shared clock: `_get_timeout` subtracts elapsed time since
the search began, so a cold pool spends the whole `outgoing.request_timeout` on concurrent DNS+TLS.
The signature is `search duration : 3.20xx s` repeating across unrelated engines.

Only the running webapp shows it: curl, httpx and `searx.network` by hand all succeed, and
restarting does not help. A warm pool masks it, so test with unused terms — `hermes-doctor web`
uses random rare words.

Repeated timeouts then trip engine suspension (`suspended_time=180`), reported as
`Suspended: timeout`.

## Chat platforms

A platform starts when its credentials are in that home's `.env` — `TELEGRAM_BOT_TOKEN`, or
`SIGNAL_HTTP_URL` + `SIGNAL_ACCOUNT`. The `platforms:` block in `config.yaml` maps toolsets, not
startup. Disabling one means commenting its vars in root and in every profile.

The gateway is a user unit — `systemctl --user restart hermes-gateway.service` — and reads `.env`
only at start.

A disabled platform keeps its last `gateway_state.json` row, `needs_attention` included. Test
whether that row's `writer_pid` is alive: the gateway is several processes and a child writes the
rows, so comparing against the gateway's own pid marks every live platform stale.

## Session transcripts

`~/.hermes/state.db` holds every session, written live. Telegram rows carry `source='telegram'`.

`hermes sessions export --source telegram` prints "Exported 0" and exits 0: export reuses the prune
selector, whose WHERE begins `ended_at IS NOT NULL`, and `session_reset.mode: none` keeps a chat
session open forever. `--session-id` bypasses the guard:

```bash
hermes-doctor sessions --source telegram      # find the id
hermes-doctor sessions --export <id>
```

## Editing hermes config

- Back up first: `hermes plugins enable` strips every comment in `config.yaml`.
- Edit YAML as text. A round-trip drops every comment.
- `.env` is `600` and holds live credentials. Preserve the mode.
- Never rewrite a running script: bash reads by byte offset.
