# Langfuse Spike

## Recommendation

Langfuse is the best next observability step for Jarvis.

It fits the current stack better than LangSmith because Jarvis is already:

- local-first for operational logs
- OpenRouter-based rather than LangChain-native
- light on framework abstractions
- likely to benefit from open standards and optional self-hosting

## Why It Fits

Jarvis currently records:

- LLM usage and cost estimates in `data/llm_activity.jsonl`
- runtime activity in `data/ops_activity.jsonl`
- errors in `data/ops_issues.jsonl`
- audit events in `data/ops_audit.jsonl`

That is enough for lightweight local debugging, but not enough for:

- request-level traces across agent, tools, and external APIs
- prompt/version comparisons over time
- model-quality review workflows
- richer dashboards and alerts

Langfuse adds those without forcing a broader application rewrite.

## Comparison

### Langfuse

- Strong match for OpenTelemetry-based tracing
- Works well with custom Python apps
- Self-hosted and cloud options
- Good prompt and evaluation features without requiring LangChain

### LangSmith

- Strong hosted tracing UX and evaluation tooling
- Best fit when the app is deeply coupled to LangChain or the team wants a SaaS-first workflow
- Less attractive for Jarvis because it adds a stronger vendor dependency than the current architecture needs

### Opik

- Viable alternative, especially if evaluation becomes the main priority
- Less obviously aligned than Langfuse for this repo's current shape
- Worth revisiting if evaluation datasets and review pipelines become the main bottleneck

## Proposed Integration Scope

Start small:

1. Add optional Langfuse env vars and a tiny client wrapper.
2. Emit traces around:
   - `JarvisAgent.chat`
   - structured-output classification calls
   - document extraction and anonymization handoff
   - reminder delivery and Telegram callback actions
3. Attach metadata already available locally:
   - model
   - latency
   - token usage
   - estimated cost
   - operation id
   - task/channel names
4. Keep local JSONL logging in place as the low-dependency fallback.

## Non-Goals For First Pass

- full replacement of local observability files
- broad instrumentation across every helper function
- blocking the app when Langfuse is unavailable

## Decision

Proceed with Langfuse when we want hosted or self-hosted trace visibility beyond local JSONL logs.
Skip LangSmith for now.
