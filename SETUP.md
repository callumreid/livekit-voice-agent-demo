# LiveKit Voice Agent — Coval SIP Setup

This document covers everything done to transform this repo from a wagyu-cattle demo agent into a working Coval inbound-voice agent reachable via SIP.

---

## What Was Changed

### `agent.py`
- Replaced the wagyu cattle persona with a generic helpful assistant prompt matching the pipecat agent
- Added 4 `@function_tool()` methods: `get_current_time`, `get_weather`, `search_web`, `lookup_order_status`
- Changed STT/TTS from AssemblyAI + Cartesia to Deepgram (matching pipecat)
- Changed LLM from `gpt-4.1-mini` to `gpt-4o-mini`
- **Critical:** Used `deepgram.STT()` and `deepgram.TTS()` plugin instances directly instead of the `"deepgram/..."` string shorthand — LiveKit Cloud's inference proxy rejects all Deepgram model names. Direct plugin calls hit Deepgram's API using `DEEPGRAM_API_KEY`.
- Added `agent_name="livekit-voice-agent"` to `@server.rtc_session()` so the SIP dispatch rule can target it by name

### `pyproject.toml`
- Swapped `livekit-agents` extras from `[assemblyai,cartesia]` to `[silero,turn-detector,deepgram]`
- Added `duckduckgo-search>=8.1.1` for the `search_web` tool

### `livekit.toml`
- Added `[build] image` pointing to the Docker image on Docker Hub so `lk agent deploy` knows what to push to LiveKit Cloud

---

## Deployment

The agent runs on **LiveKit Cloud** (project `testproj`, subdomain `testproj-idq4nqwp`).

```bash
# Build and push Docker image (linux/amd64 for LiveKit Cloud)
docker buildx build --platform linux/amd64 --push -t callumcoval/coval-livekit-agent:latest .

# Deploy to LiveKit Cloud (reads livekit.toml)
lk agent deploy

# Restart to pick up new version immediately
lk agent restart
```

Agent ID: `CA_REezQgt5v7MH`

Secrets set on the agent (via `lk agent update-secrets`):
- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`

`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` are injected automatically by LiveKit Cloud.

---

## SIP Configuration

LiveKit Cloud has a built-in SIP gateway — no separate webhook server needed.

### SIP Trunk
- **Trunk ID:** `ST_nPdedjeMpu23`
- **Numbers:** `["agent"]`
- **Allowed addresses:** `["0.0.0.0/0", "::/0"]` — **both IPv4 and IPv6 CIDRs are required**; using only `0.0.0.0/0` causes `404 No trunk found` because LiveKit's SIP gateway uses IPv6

### Dispatch Rule
- **Rule ID:** `SDR_BGnuQTpN6sC7`
- **Type:** Individual (one room per call)
- **Target agent:** `livekit-voice-agent`

### SIP URI
```
sip:agent@37s9te18ngw.sip.livekit.cloud
```

> **Note:** The correct SIP domain is derived from the LiveKit **project ID** (`p_37s9te18ngw`), not the project subdomain (`testproj-idq4nqwp`). Strip the `p_` prefix. The subdomain-based domain (`testproj-idq4nqwp.sip.livekit.cloud`) returns `404 No trunk found`.

---

## Coval Agent

- **Agent ID:** `c7ZsqmjugUKacsRMxBHkrx`
- **Type:** Inbound Voice
- **Phone number / SIP address:** `sip:agent@37s9te18ngw.sip.livekit.cloud`

To run a simulation, point any Coval run at agent `c7ZsqmjugUKacsRMxBHkrx` with any voice persona.

---

## Gotchas Encountered

| Issue | Symptom | Fix |
|-------|---------|-----|
| Wrong SIP domain | `404 No trunk found` on all SIP INVITEs | Use `{project_id_without_p_}.sip.livekit.cloud`, not the subdomain |
| IPv6 not in trunk `allowed_addresses` | `404 No trunk found` | Add `::/0` alongside `0.0.0.0/0` |
| TTS string shorthand | `model not found for provider: deepgram, model: aura-asteria-en` — agent joins room but never speaks | Use `deepgram.TTS(model="aura-asteria-en")` directly; LiveKit Cloud's inference proxy does not support any Deepgram model names |
| Agent cold start | First call after inactivity takes 60–120s before agent greets | Once the agent handles a call it stays warm; consider setting min instances > 0 in the LiveKit Cloud dashboard |
| `duckduckgo_search` rename | `RuntimeWarning: package renamed to ddgs` | Cosmetic only — search still works. Update dep to `ddgs` when convenient |
