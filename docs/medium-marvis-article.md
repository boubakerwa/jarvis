# Building Marvis: A Local AI Assistant With Claude-Style Agentic Infrastructure

*What it looks like to build a personal assistant that does real work across Telegram, Gmail, Google Drive, memory, and calendar tools, without turning the whole system into prompt soup.*

Repo: [github.com/boubakerwa/jarvis](https://github.com/boubakerwa/jarvis)

Most personal AI assistants look impressive right up until you ask them to touch a real system.

Chat is easy. Trust is hard.

That was the starting point for **Marvis**: a local assistant that runs on a Mac Mini, talks to me through Telegram, watches Gmail for useful documents, files them into Google Drive, remembers the things that matter, and can create tasks and calendar events without pretending the model should own the whole world.

The interesting part of Marvis is not that it "uses AI." The interesting part is that it is built around a clean, Claude-shaped agent loop with deterministic boundaries around everything that can go wrong.

That made it a surprisingly good exhibit for modern agent engineering.

## What Marvis Actually Does

In day-to-day use, Marvis behaves like a compact personal ops layer:

- I can message it on Telegram and ask it questions about my projects, habits, documents, or schedule.
- It stores durable memory in a structured way instead of hiding everything inside chat history.
- It watches Gmail, filters out noise, and files meaningful attachments into a fixed Drive structure.
- It can create tasks and calendar events.
- It exposes a small local dashboard so I can inspect memory, Drive files, and activity logs.

That mix matters. It means the system is not just a chatbot with a few demo tools. It is a real agentic application with both foreground interactions and background workflows.

## The Core Idea: Keep The Loop Claude-Shaped

The central design decision was simple: keep the reasoning loop close to Anthropic's Messages API and tool-use model.

Marvis uses the Anthropic Python client and the same basic contract that makes Claude-based agents feel sane to build:

- a `system` prompt
- a `messages` array
- a defined `tools` surface
- assistant-side `tool_use`
- app-side `tool_result`

In Marvis, that loop is currently routed through OpenRouter's Anthropic-compatible endpoint, which turned out to be a practical compromise. I get Claude-style message semantics and tool orchestration, while keeping the transport flexible enough to validate other models on lower-risk tasks.

That distinction is worth being explicit about. The app is not built around a generic "LLM abstraction" that hides everything. It is built around a Claude-native interaction pattern, and that has been a feature, not a limitation.

![Placeholder for an editorial-quality architecture diagram of Marvis. Show a Telegram user entering through a Telegram Bot into a Marvis Agent Loop. Above the agent loop, show Prompt Builder and Memory Retrieval pulling from SQLite and ChromaDB. Below the loop, show tool branches to Calendar, Tasks, Drive search, and memory writes. On the right, show OpenRouter as the transport layer serving Anthropic-style Messages requests, with Claude Sonnet as the default model and optional Gemma routing only for lower-risk tasks like relevance and financial extraction. Underneath, show a Gmail watcher pipeline that parses unread emails, runs relevance filtering, classifies attachments, uploads files to Google Drive, and creates memory records. At the bottom, show a local dashboard reading memories, Drive files, and activity logs. Style should be elegant, publication-ready, technical, and easy to scan, with labeled arrows and a local-first feel.](TODO-marvis-overview-diagram.png)

## Why This Matters For Agentic Systems

The biggest trap in agent design is assuming the model should be both the planner and the source of truth.

That is usually where systems become fragile.

In Marvis, the model is responsible for interpretation, planning, and selecting tools. The application is responsible for everything that must remain dependable:

- authentication and API calls
- storage and folder structure
- memory persistence
- schema validation
- time resolution
- Gmail polling rules
- logging and operator visibility

This division of labor sounds conservative, but in practice it makes the assistant much more useful. The model gets to do the high-level reasoning work, while the application keeps side effects grounded.

That pattern also makes failures easier to debug. When something goes wrong, I can inspect the logs, the dashboard, the memory table, and the Drive state instead of asking the model to explain its own behavior after the fact.

## Memory That Is Useful, Not Mystical

One of the cleaner parts of the system is the memory layer.

Marvis stores memory in SQLite as structured records and uses ChromaDB only for semantic retrieval. Before each model call, it retrieves the most relevant memories and injects them into the prompt. That gives the assistant continuity, but keeps the durable truth outside the model.

What I like most about this design is that memory stays inspectable. I can open the dashboard and see what Marvis currently believes about me: preferences, facts, decisions, project notes, document references. That is a much healthier pattern than calling something "memory" when it is really just hidden conversation state.

This small choice changes the product feel. The assistant becomes easier to trust because it is easier to audit.

## Gmail Filing Turned Out To Be The Best Agent Workflow

The background Gmail pipeline is where Marvis stopped feeling like a chat app and started feeling like a real system.

Every few minutes, it checks unread mail after a configured cutoff date. It parses the email, asks whether the message is worth storing, and only then proceeds into attachment extraction, classification, Drive upload, and memory creation.

That pipeline does something deceptively valuable: it turns messy inbox traffic into retrievable personal context.

But it also revealed an important lesson. Agentic workflows are not just about whether the model can "use tools." They are about what the application is willing to trust.

If relevance filtering fails, Marvis prefers filing over silent loss. If structured JSON comes back malformed or semantically off, the app validates it before accepting it. If a task is lower-risk, it can eventually use a cheaper or more experimental model. If it is critical, it stays on the safer path.

![Placeholder for a polished systems diagram focused on Marvis Gmail processing. Show unread Gmail messages entering a cutoff-date filter, then Email Parser, then Relevance Filter, then branching into skip or file. The file path should continue through attachment extraction, text extraction for PDFs, DOCX, and images, attachment classification, optional financial extraction, Google Drive upload into a fixed personal folder tree, and memory record creation with document references. Include callouts that structured outputs are validated after parsing, Claude is the default model, and lower-risk subtasks can be routed differently. The visual should feel like a real engineering diagram for a production workflow, not a toy flowchart.](TODO-marvis-gmail-pipeline-diagram.png)

## The Most Important Fix Wasn’t A Better Prompt

One of the most useful lessons in this project came from a failure.

I asked Marvis to create a reminder for "Monday." It said the task had been created. Later I discovered it had indeed created the event, but on the wrong Monday, in the wrong year.

That bug was important because it exposed a subtle truth about agent systems: many failures are not spectacular. They are plausible. The model returns something that looks correct enough to pass through, and the system quietly mutates the world in the wrong way.

The fix was not to write a longer prompt about dates.

The fix was to move relative time resolution into application code.

Now, expressions like "today," "tomorrow," and "Monday" are resolved locally and deterministically before calendar or task tools act on them. That one change improved reliability far more than any amount of prompt tweaking would have.

I keep coming back to the same principle:

> The model can help interpret intent, but the application should decide what counts as real.

## What I Learned About Using Claude-Style APIs

The reason I still like Claude-style agent infrastructure is not branding. It is ergonomics.

The Messages format, the explicit tool schema, and the `tool_use` / `tool_result` round trip create a mental model that maps well to real software. It is easier to reason about than giant framework abstractions, and easier to harden because the seams are visible.

Marvis reinforced a few things for me:

- Native tool semantics are worth leaning into.
- Memory should be external, structured, and inspectable.
- Structured outputs should always be validated before they can trigger side effects.
- A dashboard is not a vanity feature. It is part of the safety model.
- Deterministic code should own dates, storage, and irreversible actions.

This is also why routing through OpenRouter did not change the architectural story. The transport can move, but the contract stays the same. If I swapped the endpoint back to Anthropic directly, the design would still hold because the app is already organized around Claude-shaped concepts.

## Why Marvis Feels Like A Good "Exhibit" Project

There are plenty of projects that demonstrate a model. Fewer demonstrate an operating pattern.

Marvis is useful in that second sense.

It shows what happens when you combine:

- a compact tool surface
- a stable message contract
- background automations
- durable memory
- explicit validation
- local observability

That combination is where agentic systems start to feel less like demos and more like software.

And maybe that is the most interesting takeaway from building it: the magic is not in making the assistant feel unbounded. The magic is in deciding exactly where it should stop being magical.

## References And Useful Entry Points

If you want to explore the project, these are the best places to start:

- Repository: [https://github.com/boubakerwa/jarvis](https://github.com/boubakerwa/jarvis)
- Local docs: `docs/index.html`
- Agent loop: `core/agent.py`
- Shared LLM client: `core/llm_client.py`
- Prompt construction: `core/prompts.py`
- Deterministic time handling: `core/time_utils.py`
- Gmail pipeline: `gmail/watcher.py`, `gmail/relevance.py`, `agent_sdk/filer.py`
- Dashboard: `dashboard/app.py`

Marvis is still a personal system, but that is exactly why it is useful as a reference. It touches real tools, accumulates real state, and exposes the engineering choices that matter once an agent stops being a toy.
