---
id: model-integrations
title: Model integrations
sidebar_position: 4
---

# Model integrations

Reclio uses a small LLM for row titles. Four providers are supported:

| Provider | Best for | Cost | Setup |
| --- | --- | --- | --- |
| **Ollama** (default) | Self-hosting, no API fees | Free, local | Docker Compose brings it up for you |
| **Claude** | Highest-quality titles, hosted | Anthropic pricing | Set `ANTHROPIC_API_KEY` |
| **OpenAI** | Existing OpenAI plan | OpenAI pricing | Set `OPENAI_API_KEY` |
| **None** | Minimal deployments | Free | Set `LLM_PROVIDER=none` |

Switching is a single env var — no code changes, no rebuilds. The
rest of Reclio doesn't care which model answers.

## Ollama (default)

Runs inside your Compose stack. On first boot it pulls
`llama3.2:3b` (~2 GB). Subsequent starts are cached.

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2:3b
```

Swap `OLLAMA_MODEL` for anything in the Ollama library — e.g.
`qwen2.5:3b`, `phi3:mini`. Reclio calls `POST /api/pull` on startup so
the model is ready before the first `/feeds` hit.

## Claude

Best title quality, no infrastructure. Titles are cached for 24h so
cost per user stays low.

```env
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-haiku-4-5
```

If `ANTHROPIC_API_KEY` is blank, Reclio silently falls back to the
null provider (plain f-strings). It does not crash.

## OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

`gpt-4o-mini` is the sensible default — title generation is a short,
cheap completion. Bigger models don't produce meaningfully better
output.

## None

If you don't want any LLM calls at all:

```env
LLM_PROVIDER=none
```

BYW rows then become `"Because you watched Interstellar"` via a
straight f-string, which is also what every provider falls back to on
error.

## What the LLM actually generates

Two things, and only two:

1. **Because-you-watched titles** — turning `"Interstellar"` into a
   4–8 word row heading like *"Ever venture out to space?"*.
2. **Optional section blurbs** — not shown to Chillio users today,
   reserved for a future web dashboard.

## Prompt-injection mitigation

User-controlled strings (movie titles from Trakt metadata, show
names) are passed through `sanitize_for_prompt()` before being
interpolated into the prompt:

- Control characters collapsed to spaces.
- Backslashes and quotes stripped.
- Hard cap at 120 characters.

If the LLM returns anything suspicious (empty, overly long, multiple
lines), Reclio discards it and uses the fallback.
