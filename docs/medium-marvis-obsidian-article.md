# Building Marvis: Why Obsidian Became The Collaboration Layer

*A sequel to the first Marvis article, about the quiet little miracle of letting notes stay notes.*

Repo: [github.com/boubakerwa/jarvis](https://github.com/boubakerwa/jarvis)

The first Marvis article was about the agent loop: Telegram in, tools out, structured memory, Gmail automation, Drive filing, and the occasional reminder that software should probably behave itself.

This one is about the quieter part of the system.

The part where ideas turn into notes, notes turn into working drafts, and nobody has to pretend that a markdown file is suddenly a sacred source of truth.

For Marvis, that place is Obsidian.

Not because Obsidian is the database. Not because markdown is magical. And definitely not because I enjoy turning every thought into a neatly labeled artifact for the vibes.

Obsidian became useful because it sits in the right spot: close enough to the work to be collaborative, but not so central that it starts acting like the boss.

That distinction matters more than I expected.

## Why Marvis Needed A Notes Layer At All

Marvis already had structured memory.

It stores durable facts, preferences, decisions, and document references in a form the system can query and reason over. That is the part of the assistant that needs to be dependable.

But structured memory alone is not a good collaboration surface.

Sometimes I want a note that reads like a note, not a record. Sometimes the useful thing is a loose plan, a meeting recap, a draft, a running checklist, or a half-finished idea that still needs human editing. Sometimes the right output is not "store this as memory" but "give me something I can keep working on."

That is where Obsidian fits.

It gives Marvis a place to write Markdown the way a person would want to read it later. It also gives me a place to see the assistant’s output without having to dig through logs or model messages.

In practice, that means Obsidian acts as a collaboration layer:

- durable enough to keep
- editable enough to refine
- local enough to feel owned
- simple enough that the assistant can operate on it without inventing a new format

## The Mental Model: Memory Is The Truth, Notes Are The Workspace

The most important design choice in this part of the system is that notes are not the source of truth.

They are adjacent to the source of truth.

That might sound minor, but it changes everything.

If a note vault becomes the primary memory store, you start inheriting all the problems of freeform text as a database. You get ambiguity, duplication, accidental overwrite risk, and a lot of hidden structure that only exists in the model’s head.

Marvis avoids that by keeping the responsibilities separate:

- structured memory stores the stable facts the agent should rely on
- Obsidian stores the human-readable layer around those facts
- the model can bridge the two, but it does not own either one completely

The result is healthier than a typical "AI note-taking" setup, because the assistant is helping me think and organize, not quietly replacing the record-keeping system.

That separation is visible in the code too. The notes path in `notes/service.py` is intentionally narrow: create a note, append to a note, update a note, search notes, read a note, list recent notes. The lower-level vault wrapper in `notes/obsidian.py` handles file operations, slugging, path resolution, and basic safety.

That sounds boring.

It is.

Which, in software, is often a compliment.

## Local-First Collaboration Feels Different

I care a lot about tools that stay legible when you are tired, mildly distracted, or operating on the emotional horsepower of a sleepy raccoon.

That is one reason Obsidian fit Marvis so naturally. It is local-first, plain-text, and inspectable. If the assistant writes something useful, I can open the file directly. If I do not like the shape of the note, I can edit it by hand. If I want to move fast, I can let Marvis draft the thing and then clean it up later.

There is no vendor-shaped ceremony in the middle, no mysterious sync ritual, and no moment where a note turns into a hostage situation.

That matters for a personal assistant because a lot of the value is not in one-shot output. The value is in repeated small interactions:

- turn a messy thought into a draft note
- append follow-up context after a call
- capture a decision while it is still fresh
- search a few recent notes when I need continuity
- keep the note format simple enough that future me will actually use it

This is also why the assistant uses Markdown rather than a proprietary note format. Markdown is not fancy, but it is resilient. A note written today still makes sense later, even if the rest of the system changes around it.

## What The Notes Layer Actually Does

Marvis does not treat Obsidian as a passive dump folder.

It can create notes, append to existing ones, replace text in place, search recent content, and keep file paths predictable. That makes notes feel like live artifacts instead of static exports.

The important part is that these operations are intentionally small.

Marvis is not trying to become a full knowledge management platform. It is trying to support a workflow where the assistant can:

- capture a draft quickly
- organize it into a sensible folder
- update it when new context arrives
- leave a trace of the change

That narrow scope keeps the integration useful.

In a system like this, restraint is a feature.

You want enough structure that the assistant can reliably place and update notes, but not so much structure that every note becomes a schema migration problem.

## Auditability Is Part Of The Product

One thing I care about more and more in Marvis is being able to answer a simple question:

What changed, and why?

That is especially important when a system can mutate files on your behalf.

Marvis records note operations through ops logging, which means note creation and updates are not invisible side effects. They show up as audit events. That gives the whole setup a much calmer feel, because the assistant is no longer doing "mystical" work behind the curtain.

I can inspect the outcome later.

I can tell whether a note was created, appended, or replaced.

I can infer whether the model made a sensible choice or whether I should tighten a prompt or a guardrail.

That kind of traceability is easy to undervalue until you need it. Then it becomes one of the most important parts of the product.

It is also a good reminder that observability is not just for servers. Personal systems need it too, unless you enjoy playing detective against your own tools.

## Obsidian Helps Marvis Stay Human

There is a temptation when building agentic software to make everything look like a command interface.

That is sometimes useful. It is rarely pleasant.

Obsidian keeps Marvis from becoming too cold or too transactional. Notes can be rough, informal, unfinished, or collaborative. They can feel like something I would actually keep using after the novelty wears off.

That matters because the best personal systems do not just reduce effort. They preserve texture, which is a fancy way of saying "please do not sand all the personality out of my workflow."

A good assistant should help you move faster without flattening your thinking into structured objects too early. Sometimes a note starts as a loose paragraph and becomes a decision later. Sometimes it starts as a list and ends up being a permanent reference page. Sometimes it remains messy, and that is fine.

The notes layer gives Marvis permission to meet me where I am.

## Where The Boundaries Stay Sharp

The real trick is knowing what not to let Obsidian do.

It should not silently become the authoritative memory store. It should not be the place where hidden state accumulates with no review path. It should not turn into a second, informal database that the rest of the system has to guess at.

That is why the architecture keeps memory and notes distinct:

- memory is for durable, queryable facts
- notes are for collaboration, drafting, and human review
- audit logs are for accountability
- the agent loop is for interpretation and tool selection

That split makes the assistant easier to reason about and easier to trust.

It also keeps the system flexible. If I want to change the note structure later, I can. If I want to move the vault, I can. If I want to rewrite how memory is stored, the note surface does not collapse with it.

The pieces are connected, but they are not entangled.

## A Better Way To Think About "AI Notes"

The phrase "AI notes" often implies that the model writes the note and therefore owns the note.

I think that framing is backwards.

The model should help produce the note.
The application should protect the note.
The human should remain able to read and edit the note.

That is the version of the pattern that feels sustainable.

In Marvis, Obsidian is not a gimmick and it is not an afterthought. It is the collaboration surface that lets the assistant participate in my workflow without trying to become the workflow itself, which is a surprisingly important distinction when a model has access to a keyboard.

That is the kind of integration I care about most:

- useful enough to reach for
- local enough to trust
- simple enough to audit
- flexible enough to evolve

## Closing Thought

The most interesting part of building Marvis has not been teaching it to write more.

It has been deciding where writing should happen, where truth should live, and what the assistant should be allowed to own.

Obsidian turned out to be the right answer for the middle layer. Not the memory layer. Not the reasoning layer. The middle.

And for a personal assistant, that middle is where a lot of the real work happens.

## References And Useful Entry Points

If you want to explore the relevant pieces of the repo, start here:

- Notes manager: `notes/service.py`
- Vault operations: `notes/obsidian.py`
- Telegram agent entry point: `telegram_bot/bot.py`
- Shared agent loop: `core/agent.py`
- Ops logging: `core/opslog.py`
- Main Gmail processing path: `main.py`
- First article: `docs/medium-marvis-article.md`

Marvis keeps evolving, but the principle behind the notes layer has stayed steady: use notes to collaborate, use memory to remember, and use logs to stay honest about what changed.
