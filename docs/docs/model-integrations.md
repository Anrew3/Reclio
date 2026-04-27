---
id: model-integrations
title: Model integrations
sidebar_position: 4
---

# Model integrations

Reclio uses **two model workers** — one for chat completions, one for
embeddings. They're configured independently so you can mix and match
(e.g. chat on Claude for variety, embeddings on OpenAI for top-tier
quality). See [Embeddings](./embeddings) for the full embedding-side
breakdown; this page covers the chat side.

Five chat providers are supported via `LLM_PROVIDER`:

| Provider | Best for | Cost | Setup |
| --- | --- | --- | --- |
| **Ollama** (default) | Self-hosting, no API fees | Free, local | Docker Compose brings it up for you |
| **Claude** | Highest-quality replies, hosted | Anthropic pricing | Set `ANTHROPIC_API_KEY` |
| **OpenAI** | Existing OpenAI plan | OpenAI pricing | Set `OPENAI_API_KEY` |
| **OpenRouter** ✨ | One key → ~200 models (Claude, GPT, Llama, Gemini, Qwen, Mixtral…) | OpenRouter passthrough pricing | Set `OPENROUTER_API_KEY` |
| **None** | Minimal deployments | Free | Set `LLM_PROVIDER=none` |

Switching is a single env var — no code changes, no rebuilds. The rest
of Reclio doesn't care which model answers.

## What the chat LLM is used for

Five places, in roughly increasing importance:

1. **Because-You-Watched titles** — *"Ever venture out to space?"*
   instead of bare *"Because You Watched Interstellar"*. Cached 24h.
2. **Personality summary** — the playful 1-line roast at the top of the
   dashboard's "What Reclio thinks about you" card. Regenerated each
   taste-profile rebuild.
3. **Ask Reclio chat** — the floating bubble. Chat replies + intent
   classification (so "stop showing me horror" actually mutates your
   preferences).
4. **Onboarding preference derivation** — turns five free-form answers
   into a structured profile (mood tags, excluded genres, era prefs,
   family-safe flag, vibe summary). One-time per user.
5. **Conversational mutations** — "newer movies please" / "less
   action" / "never recommend Inception again" all flow through the
   classifier into structured preference updates.

## Ollama (default)

Runs inside your Compose stack. On first boot it pulls
`llama3.2:3b` (~2 GB). Subsequent starts are cached.

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2:3b
```

Swap `OLLAMA_MODEL` for anything in the Ollama library — e.g.
`qwen2.5:3b`, `phi3:mini`. Reclio calls `POST /api/pull` on startup
so the model is ready before the first `/feeds` hit.

If you also want Ollama to serve embeddings, pull the embedding
model separately:

```bash
docker compose exec reclio-ollama ollama pull nomic-embed-text
```

## Claude

```env
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-haiku-4-5
```

If `ANTHROPIC_API_KEY` is blank, Reclio silently falls back to the
NullProvider. Anthropic ships no embedding models — so when chat is
on Claude, embeddings auto-fall-back to local sentence-transformers
unless you set `EMBEDDING_PROVIDER=openai`.

## OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

When chat is on OpenAI, embeddings automatically use OpenAI
`text-embedding-3-small` (1536d) too — same key, same vendor, top-tier
embedding quality. This is the highest-quality single-vendor
configuration available.

## OpenRouter ✨ (added in v1.6)

OpenRouter is a router — one API key gets you access to ~200 chat
models from every major vendor (Anthropic, OpenAI, Meta Llama,
Google Gemini, Mistral, Qwen, DeepSeek, etc.).

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=anthropic/claude-3.5-haiku
```

Model names follow OpenRouter's `vendor/model` convention. Some
notable picks:

| Model | When |
|---|---|
| `anthropic/claude-3.5-haiku` (default) | Cheap, fast, good baseline |
| `anthropic/claude-3.5-sonnet` | Highest quality, ~10× the cost |
| `openai/gpt-4o-mini` | OpenAI alternative without an OpenAI key |
| `meta-llama/llama-3.3-70b-instruct` | Open weights, often very fast on Groq backends |
| `google/gemini-2.0-flash-exp:free` | Free tier, good for testing |
| `meta-llama/llama-3.2-3b-instruct:free` | Smallest free option |

OpenRouter does **not** proxy embeddings. When `LLM_PROVIDER=openrouter`,
embeddings fall back to local sentence-transformers. To get OpenAI
embeddings alongside OpenRouter chat, set `EMBEDDING_PROVIDER=openai`
+ `OPENAI_API_KEY`. See [Embeddings](./embeddings).

## None

If you don't want any LLM calls at all:

```env
LLM_PROVIDER=none
```

BYW row titles become straight f-strings (*"Because You Watched
Interstellar"*). The Ask Reclio chat shows a "chat offline" state.
Personality blurbs are skipped (the donut + bars still render). All
LLM-dependent features degrade gracefully — `/feeds` still returns
10 valid rows.

## Mix-and-match: chat ≠ embeddings

The two workers are configured independently:

```env
# Chat on OpenRouter (model variety)
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet

# Embeddings on OpenAI (top-tier 1536d quality)
EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

Allowed `EMBEDDING_PROVIDER` values: `auto` (follows `LLM_PROVIDER`,
default) · `openai` · `ollama` · `local` (sentence-transformers) ·
`none`. See [Embeddings](./embeddings) for the full provider matrix
and quality-vs-cost comparison.

## Prompt-injection mitigation

User-controlled strings (Trakt usernames, movie titles, free-form
chat input) are passed through `sanitize_for_prompt()` before being
interpolated into the prompt:

- Control characters collapsed to spaces
- Backslashes + quotes stripped
- Length-capped per call (titles 120 char, chat 400-500 char)

If the LLM returns anything suspicious (empty, overly long, malformed
JSON for the structured calls), Reclio discards it and uses the
fallback path. The classifier in particular runs strict JSON parsing
+ field-level whitelisting before applying any preference mutations.
