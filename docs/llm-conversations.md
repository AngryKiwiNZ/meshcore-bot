# LLM conversations

LLM conversations are optional. The bot can use a compatible remote chat provider
and automatically fall back to a local Ollama model when configured. Disable the
feature entirely with `[LLM] enabled = false`.

## Privacy model

Credentials, contact profiles, conversation history, runtime status, and databases
are local runtime data. They are stored under `data/` (or paths selected in local
configuration) and are ignored by Git. Do not commit them, copy them into examples,
or publish screenshots that expose them.

Remote-provider requests can include the system prompt, the authorised contact's
profile notes, and a bounded conversation history. Choose local-only mode whenever
that information must stay on the host. The protected Conversations console explains
which provider is active and lets an operator clear histories and remove a saved key.

## Routing and access control

- Registered bot commands keep priority over conversational replies.
- Direct-message access can be enabled independently from channel access.
- Channel conversations require a configured bot mention; unrelated channel traffic
  never invokes the LLM.
- Contact profiles are opt-in and can be scoped to `DMs only` or `All messages`.
- Personal-radio access is fail-closed and requires explicit authorisation.
- Natural-language tool use is restricted to the configured allowlist of read-only
  bot commands. Administrative and mutating commands are never exposed.

## Operations

Only one model request runs at a time. A small bounded queue protects low-powered
hosts and preserves reply order. Conversation sessions, history retention, response
size, chunk size, and timeouts are configurable in the local `[LLM]` section.

The protected console exposes provider health, fallback activity, queue depth, and
model residency. It also provides a global kill switch. Keep the web console bound
to localhost or protect it with a reverse proxy, TLS, and strong credentials.

## Local Ollama fallback

Install and fetch the model selected in your local configuration, for example:

```bash
ollama pull <model-name>
```

Set `fallback_to_ollama = true` only after confirming Ollama is reachable at the
configured local URL. The bot will use it when the remote provider is unavailable or
returns an error.
