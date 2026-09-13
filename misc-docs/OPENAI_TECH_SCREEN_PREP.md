# OpenAI Hiring Manager / Tech Screen — preparation plan

**Format, per Mads' email:** collaborative, discussion-based exercise on an
*ambiguous* technical problem in agentic system architecture. You will be asked
to clarify requirements, break the problem down, discuss design choices and
tradeoffs, and explain how you would assess and improve the solution in
practice. Explicitly: no coding, no advance preparation required. They are
scoring **technical judgment, structured thinking, communication**.

**One gap:** I could not read the JD — your corporate Zscaler proxy intercepts
the Ashby link and returns a gateway redirect. Everything below is built on the
email, which is the stronger signal anyway (it describes the actual exercise).
Paste the JD text and I'll sharpen §5 and §7; the rest is title-independent.

---

## 0. The uncomfortable truth — read this twice

**"No advance preparation is required" is not politeness. It is a description of
the scoring rubric.** They have designed an exercise where prepared answers
cannot help, because the problem will be underspecified on purpose. What is
being measured is what you do in the first five minutes when you do not have
enough information.

**Your specific failure mode is the one this format punishes hardest.** You have
a deep, genuinely impressive project with 30 ADRs, and the natural instinct is to
route every prompt through it — "that's like what I did with the dependency
miner." In an ambiguity exercise that reads as pattern-matching instead of
thinking, and worse, it *skips the part they are scoring*. The candidate who says
"before I propose anything, what decision is this system supposed to enable?" beats
the candidate who proposes a better architecture in minute two.

**So rehearse a method, not answers.** Two things are worth committing to memory:

1. **The opening sequence** (§1) — a repeatable way to attack an ambiguous
   agentic problem. Practise this out loud until it is automatic.
2. **Eight compressed primitives** (§4) — 30–45 seconds each, deployed as
   *evidence* when the conversation reaches them, never as an opening move.

Everything else in this document is context so you are not surprised.

---

## 1. The method — how to run the first twenty minutes

Say the step names out loud. Narrating your structure *is* the "structured
thinking" signal; they cannot read your mind.

### Step 1 — Play it back, then find the decision (60 seconds)

> "Let me play back what I heard to make sure I've got it… Before I design
> anything, the question I want answered first is: **what decision does this
> system exist to enable, and who makes it?**"

Ambiguous prompts almost always hide the *consumer* of the output. An agent that
drafts something for a human to approve and an agent that acts unsupervised are
different systems with the same description.

### Step 2 — Ask the four questions that actually change the architecture

Do not ask ten questions. Ask these four, and say why each matters:

| Question | Why it changes the design |
|---|---|
| **Who consumes the output, and what do they do next?** | Human-in-the-loop vs. autonomous changes the accuracy bar by an order of magnitude. A 90%-correct draft is useful; a 90%-correct action is a liability. |
| **What does being wrong cost — in each direction?** | The asymmetry drives everything. If a false positive costs an hour and a false negative costs an outage, you tune, retry, and escalate completely differently. Ask for the asymmetry, not the accuracy target. |
| **What is ground truth here, and can we actually obtain it?** | If you cannot evaluate it, you cannot improve it, and you should say so early. Sometimes the honest answer is "the first deliverable is a labelled set, not a system." |
| **What is the volume, latency and cost envelope?** | 100 requests/day with a 10-minute budget and 100/second with a 2-second budget are unrelated problems. Also surfaces whether this is a batch job wearing an agent costume. |

### Step 3 — Separate the reversible decisions from the irreversible ones

This is the highest-signal architect move available to you, and almost nobody
does it unprompted.

> "Most of what we'd decide here is cheap to change later. Let me name the ones
> that aren't, because those are the ones worth spending this hour on."

Typically irreversible in an agentic system:
- **The data model / what you record at capture time.** You cannot backfill
  observations you never made. (You have a hard-won example — §4.6.)
- **The trust and permission boundary.** Whether the agent can write, and what it
  can write to, propagates into every integration and every customer security
  review.
- **What you promise the customer the system does.** Withdrawing a promise costs
  more than not making it.

Everything else — model choice, prompt structure, orchestration framework, single
vs. multi-agent — is a Tuesday afternoon.

### Step 4 — Propose a deliberately boring v0, then attack it yourself

> "Here's the simplest thing that could work in two weeks. Then let me tell you
> where I expect it to break."

Two moves in one. You demonstrate scoping discipline, and you pre-empt their
critique by making it first. **Always volunteer the failure modes of your own
proposal.** It is the single strongest credibility signal in a design interview,
and it is the thing you already do naturally — lean into it.

### Step 5 — Define the assessment loop *before* the build-out

The email names this explicitly ("how you would assess and improve the solution
in practice"), so treat it as a graded section rather than an afterthought:

- **Offline:** a small labelled set — 30–50 real cases beats 500 synthetic ones —
  sliced by the dimensions you expect to fail on, wired to fail CI on regression.
- **Online:** what you log on every single run, so a production failure is
  diagnosable without a repro. Traces, tool-call sequences, token/latency/cost
  per run, and the model's own stated reasoning where available.
- **The failure taxonomy:** you cannot improve "it's wrong." Categorise —
  retrieval miss, tool misuse, bad plan, correct plan executed wrong, refusal,
  hallucinated input. Different categories have different fixes, and most teams
  skip this step and then tune prompts at random.
- **The honest caveat:** an eval suite is itself a system that can lie to you.
  You have a real example (§4.3).

### Step 6 — Say what you would cut

> "If I had half the time, I'd drop X and Y, and here's what we'd lose."

Judgment is visible in what you decline to build.

---

## 2. Likely prompts, and how to open each

These are openings, not answers. Do not memorise scripts — memorise the *first
three questions* and the trap.

### A. "A customer wants an agent to handle their support tickets end to end."

**First three questions:** What fraction of tickets are actually
resolvable without a human today, by a *person*, from the information in the
ticket? What happens when the agent is wrong — who finds out, and how fast? Do
you have the last 12 months of tickets *with their resolutions*?

**The trap:** designing the agent. The real answer is that "end to end" is almost
never the right first deliverable — you want the agent to handle the
highest-volume, lowest-consequence slice and *route* the rest, then expand the
envelope as the eval data justifies it. Frame it as widening a boundary, not as
building a system.

**The v0 spine:** classify → retrieve → draft → human approves. Measure the
approval rate. The approval rate is your eval set, generated for free by the
humans you have not yet replaced.

### B. "Design an agent that can safely operate a customer's internal tools."

**First three questions:** Which of these tools have side effects, and which are
reversible? Who is the principal — does the agent act as itself, or on behalf of
the requesting user? What is the blast radius of the worst single action the
agent can take?

**The trap:** talking about prompts and guardrails. Safety here is an
*authorisation* problem, not a prompting problem. The prompt is not a security
boundary; the credential is.

**Your strongest ground.** This is where ADR 0003 (read-only→actions boundary)
and ADR 0020 (approval gate with expiry) are directly on point — see §4.7.

### C. "A customer's agent is great in demos and fails in production. Diagnose it."

**First three questions:** What does "fails" mean — wrong, slow, expensive, or
inconsistent? Do you have traces of the failures, or only the complaints? What is
different about production inputs versus the demo inputs?

**The trap:** guessing at causes. Ask for the trace. The answer is almost always
one of: the demo inputs were curated and production inputs are messy; the context
window is filling up and the model is losing the early instructions; a tool is
returning errors the agent silently swallows; or non-determinism that was always
there and is only now visible at volume.

**Have this line ready:** "The pattern I'd look for first is silent degradation —
a dependency that fails in a way the system treats as an empty result rather than
an error." You built exactly that failure mode deliberately (§4.4) and caught it
accidentally three times (§4.6), so you can speak to both sides.

### D. "How would you evaluate an agent when there's no single right answer?"

**First three questions:** Is there a *wrong* answer we can define even if we
can't define the right one? Can we evaluate the trajectory instead of the output
— did it call the right tools in a sensible order? Who is qualified to judge, and
can we afford them?

**The trap:** jumping to LLM-as-judge. Get there, but get there *after*
establishing that the cheap deterministic checks come first: did it produce valid
output, did it stay in budget, did it avoid the tools it shouldn't touch, did it
cite something real. Rubric-based judging is for what's left, and a judge needs
its own calibration against human labels before you trust it to gate anything.

### E. "One agent with twenty tools, or five specialised agents?"

**Hold a position:** default to **one agent, fewer tools**, and make anyone
proposing multi-agent name the specific reason. Legitimate reasons exist —
different permission scopes, genuinely different context needs, independent
scaling, or a step that must be deterministic. "It feels more modular" is not
one. Multi-agent buys you decomposition and pays for it in handoff loss,
debuggability, and latency that multiplies instead of adds.

**Then invert it:** the real lever is usually **tool granularity**, not agent
count. Twenty thin tools is a planning problem you have handed to the model;
five well-chosen tools that each do a meaningful unit of work is a design you
did yourself.

### F. "The customer wants to replace a 200-person ops team with agents."

**The trap:** either enthusiasm or scepticism. Both are wrong. Ask what those 200
people actually spend their time on — the answer is usually that 70% is a small
number of repetitive flows and 30% is judgment under ambiguity, and those need
completely different treatment. Then ask who is accountable when the agent is
wrong, because that question determines whether this is a 6-month or a 3-year
programme.

---

## 3. Tradeoff positions — have a view, not "it depends"

"It depends" without a default reads as inexperience. Lead with a default, then
name what would change your mind. Each of these is a position you can actually
defend from your own work.

| Question | Your default | What changes it |
|---|---|---|
| Model in the request path, or deterministic first? | **Deterministic first, model as fallback.** If the answer is a database read, read the database. | Genuine synthesis, or an intent taxonomy too large to enumerate. |
| Single agent or multi-agent? | **Single, fewer tools.** | Different permission scopes, or a step that must be deterministic. |
| RAG, long context, or fine-tuning? | **RAG first**, because you can inspect and fix retrieval. | Long context when the corpus is small and stable; fine-tuning for *format and behaviour*, almost never for facts. |
| Prompt in code, or prompt as data? | **As data, with a gated promotion path** — prompts change on a different cadence than code, and by different people. | A tiny team with no non-engineer prompt authors; then code is simpler. |
| Can the agent write? | **Read-only until the eval story is real.** Then writes behind an approval gate with an expiry. | A reversible, bounded, low-consequence write with good audit. |
| LLM-as-judge or programmatic eval? | **Programmatic for everything you can express**, judge for the residue, and calibrate the judge against human labels before it gates anything. | — |
| Framework or hand-rolled orchestration? | **Hand-roll the loop until you know what you need** — a tool-use loop is ~50 lines and a framework is a vocabulary you then have to debug through. | Multi-tenant durability, human-in-the-loop resumption, complex fan-out. |
| Cost when it gets expensive? | **Cache the stable prefix, route the easy cases to a smaller model, and bound the tool-call loop.** In that order — caching is free, routing needs an eval to be safe. | — |

---

## 4. Your eight primitives — compressed to 30–45 seconds each

Each is tagged with the question it answers. Deploy when the conversation
arrives there. **Do not open with these.**

### 4.1 Two-tier routing — *use when: cost, latency, or "when do you use a model at all"*
Built a two-tier query path: an intent classifier routes to a deterministic
handler when the answer is a database read, and reserves inference for real
synthesis. Tier 1 falls through to Tier 2 rather than failing, so routing is an
optimisation and never a capability ceiling. Ten intents, one model call per
intent maximum. **The punchline:** the default agentic design puts a model in
every request and pays for it every time; this makes the model the fallback, so
the cost curve is bounded by the intent taxonomy rather than by traffic.

### 4.2 Prompts as gated data — *use when: LLMOps, deployment, who-owns-the-prompt*
Prompts are served from a registry, resolved per request against a `production`
label, cached in-process with a cooldown. Shipping a prompt means publishing a
*candidate*, evaluating it version-pinned, then a human promotes a label.
**The detail that shows it's real:** pinned resolution uses its own cache and
cooldown key, so a broken candidate can't suppress production's resolution or
vice versa. **The bug worth telling:** a decorator was stamping `prompt_version`
onto answers that never called a model — false provenance that would have
corrupted the promotion gate's own statistics.

### 4.3 Evals as a CI gate — *use when: "how would you assess it"*
A regression dataset spanning ten intents, run against a *deployed* backend,
failing the pipeline below a mean-score threshold, with candidates judged on the
items that actually exercise them. **Lead with the flaw:** the harness currently
collapses "quality regressed" and "every model call errored because the vendor
account is unfunded" into the same exit code. Those need different responses, so
a precondition failure should exit differently from a quality regression. Naming
a flaw in your own gate is stronger than describing a flawless one, and it shows
you know an eval suite is a system that can lie to you.

### 4.4 Semantic memory with deliberate fail-open — *use when: RAG, memory, reliability*
Incident conclusions are embedded and stored in Postgres with pgvector, retrieved
as prior context for later incidents — semantic recall over what the system has
previously *concluded*, not over documents. **The design decision:** every failure
in the embed path is silent by design — writes are async and skipped on failure,
so a rate limit degrades recall instead of breaking an incident response. That's
correct for an ops tool *and* it is a monitoring obligation, which is why the doc
has a section on verifying it actually works. Say both halves.

### 4.5 Evidence tiering — *use when: hallucination, grounding, trust, precision/recall*
**This is your strongest story and it is a failure.** I built a dependency miner
that reconstructs a service call graph from log text — no service mesh, no eBPF,
no inbound access. Every target is tiered by *what it was validated against*:
matched to a live service list, both segments matched, or validated against
nothing. The "validated against nothing" tier shipped and I disabled it the same
day: it had reported `www.nokia.com` as a dependency of the platform, harvested
from a scanner's `Referer` header, plus a scanner's `User-Agent` and two hostnames
quoted inside a vendor's own error response. **The generalisable sentence:**
*removing a validation step and replacing it with a plausibility heuristic
produces confident fabrications — the only reliable guard is checking a candidate
against a real object.* That is LLM hallucination's exact shape, in a subsystem
with no LLM in it, which is why it transfers.

### 4.6 The measured denominator — *use when: confidence, calibration, observability*
A confidence number was being read as traffic volume when it was actually
scan-cycle sightings. Rather than relabel the tooltip, I added a per-service,
per-cycle record of whether we sampled the pod, whether its logs were readable,
and how many lines came back — so confidence became *coverage*: sightings over
the service's own lifetime in scans. A low-confidence edge is now a statement
about our observation, not about the service. **The quotable arc:** three services
showed 187/187/187, which proved it couldn't be volume; then 318 vs 69 proved
absolute counts were still ambiguous; only a ratio against a measured denominator
was interpretable. **The architectural point:** this is the difference between a
system that displays a number and one that can tell you how much to trust its own
output — and you cannot backfill a denominator you never recorded.

### 4.7 The actions boundary — *use when: safety, autonomy, permissions, enterprise*
The platform can restart, scale and roll back workloads. That sits behind an
explicit read-only→actions boundary and an approval gate with an expiry on every
pending action, plus a bounded tool-iteration loop that logs non-convergence
rather than retrying forever. **The uncomfortable detail to volunteer:** the
remediation token currently treats an empty value as open. It's tracked, and it's
deliberately *not* a patch — changing the posture revises the ADR that authorised
the capability, so it needs a decision, not a hotfix. **Knowing which problems are
patches and which are decisions is the point.**

### 4.8 Egress governance — *use when: enterprise, compliance, data boundaries*
An allowlist governs which payload fields may reach the model at all — 22 fields
permitted, everything else dropped. Two Python paths talk to the Kubernetes API
and a query engine directly and never pass through the Go redactor, so they carry
a mirrored scrubber with identical patterns: same guarantee regardless of which
path answered. **The consequence that proves you traced it:** the allowlist permits
`service` but drops `images`, `replicas_desired`, `deployment`, `revision` and
`change` — so a version-skew feature *cannot* be built as an agent skill reading
through the query API. The governance boundary constrains the roadmap, which is
how you know it's a real boundary and not a document.

---

## 5. You are interviewing at OpenAI with an Anthropic-stack project

Address this head-on; do not let it be an awkward moment.

**The question you will get, in some form: "Why Claude?"** The honest answer is
also the strong one:

> "Two reasons. Practically, I had credits and wanted to go deep on one provider
> rather than shallow on three. Architecturally, I wrote the provider question
> down as a decision rather than a default — inference sits behind a thin client
> factory keyed by tenant config, and I deliberately restricted the project to the
> portable API surface: tool calling, structured outputs, prompt caching,
> reasoning. No managed-agent runtime, no server-side tools, no hosted
> conversation state. That was a conscious lock-in budget, so the provider stays a
> configuration choice and not an architectural one."

**Be precise about what that ADR does and doesn't cover** — do not oversell it.
It routes across *one model family's* deployment options (first-party, Bedrock,
Vertex) for data-residency reasons, not across vendors. The reusable part is the
*reasoning*: enumerate which API surfaces are portable and which create lock-in,
and spend your lock-in budget deliberately. That reasoning is exactly what a
customer-facing architect needs, and it is vendor-neutral.

**Translate your vocabulary before you walk in.** Using one house's terms for the
other house's primitives is a small, avoidable friction:

| What you'd say | Say this instead |
|---|---|
| Messages API | the Responses API / Chat Completions |
| tool use | function calling / tools |
| structured outputs | Structured Outputs (JSON schema) |
| extended thinking | reasoning models, reasoning effort |
| Langfuse prompt registry + datasets + traces | OpenAI Evals, traces and the prompt/eval tooling — and name Langfuse as the vendor-neutral choice you made |
| Voyage embeddings | embeddings (and say which; the pattern is identical) |

**Shared vocabulary, so use it freely:** prompt caching, MCP, function calling,
structured outputs, evals, agent loops, tool budgets, RAG, human-in-the-loop.

**Do your homework on their current agent primitives** before the call — the Agents
SDK, the Evals product, and MCP support. You do not need to have used them; you
need to be able to say "that's the equivalent of what I hand-rolled, and here's
what I'd expect to gain and lose by adopting it." That sentence demonstrates
exactly the judgment they are screening for.

---

## 6. Your specific failure modes in this interview

Honest and personalised. Each has a correction.

| Risk | Why it's a risk | Correction |
|---|---|---|
| **Kubernetes gravity** | Your project is K8s ops; the role is not. Details that feel load-bearing to you are noise to them. | Lead with the *pattern*, name the substrate second. "I had a system inferring relationships from unreliable evidence — in my case from pod logs, but the shape is the same." |
| **Over-claiming the degraded parts** | The eval gate is not currently green (unfunded account) and the embedding pipeline is not accumulating vectors (credits exhausted). One caught overstatement discredits everything else. | Say it plainly: "built and wired into CI, currently blocked on a vendor billing issue." Volunteering it is a credibility *gain*. |
| **Process as a flex** | "I have 30 ADRs" invites "so you like writing documents." | Never cite the count. Cite a *decision* from one, and what it prevented. |
| **Answering before scoping** | You are good at this material, so you will be tempted to design immediately. That skips the graded section. | Force the first 60 seconds to be playback + "what decision does this enable?" Practise it aloud. |
| **Advisor reflex, mistimed** | Disagreeing is good; disagreeing before you have understood their constraint reads as not listening. | Understand, restate, *then* disagree with structure: "I'd push back, and here's the specific risk I'm worried about." |
| **Solo-project scope inflation** | Any hint of implied team leadership is a trap you don't need. | "Solo — I own the architecture and wrote the code." That is more impressive than a vague team claim, and it's true. |
| **Ten-minute stories** | Every primitive in §4 could take five minutes. In a discussion format that is a monologue. | Land the point in 45 seconds and stop. Let them ask for depth. Silence after a crisp answer is *their* turn, not a gap for you to fill. |

---

## 7. Questions to ask them

Pick three or four. These signal seniority because each one has a real answer
that would change how you'd work.

- Where do your customers' agentic deployments most often stall — is it
  capability, evaluation, integration, or organisational trust?
- When a customer's agent underperforms, how much of the diagnosis is prompt and
  context engineering versus the surrounding system?
- How much of this role is designing the first version versus getting a stalled
  deployment unstuck? Those are different skills and I'd want to know which
  you're hiring for.
- What does the feedback loop into Product and Research actually look like in
  practice — how does a deployment constraint you hit become a roadmap item?
- What's the failure mode you see most often in candidates who do well here and
  then struggle in the role?

**Avoid:** anything answerable from the careers page, and compensation.

---

## 8. Prep schedule, and the one line to have ready

**If you have four hours total, spend them like this:**

| Time | Activity |
|---|---|
| 45 min | Practise §1 **out loud**, twice, on two different invented prompts. This is the only thing that genuinely needs rehearsal. |
| 45 min | Compress each §4 primitive to 45 seconds. Time yourself. Cut until it fits. |
| 45 min | Read OpenAI's current docs on their agent primitives and evals. Not to claim experience — to be able to compare to what you hand-rolled. |
| 30 min | Rehearse the "why Claude?" answer (§5) and the vocabulary map until it's fluent. |
| 30 min | Pick and rehearse three "tell me about a time you were wrong" stories: the fabricated dependencies, the phantom pod counts, the timezone bug your own test exposed. Each in 60 seconds, each ending in the generalisable lesson rather than the fix. |
| 30 min | Write your four questions (§7) on the notepad you'll have in front of you. |
| 15 min | Re-read §0 and §6. |

**Do not** build slides, a diagram, or a prepared architecture. If you arrive with
a solution you will try to use it, and the problem will not be the one you
prepared for.

**The single line to have ready, if you get one sentence to position yourself:**

> "I've spent the last year building an agentic operations platform solo, and the
> hardest problem was never getting the model to answer — it was stopping the
> system from reporting confident conclusions over evidence it had no right to
> trust. I have three documented cases of catching my own platform doing exactly
> that, and the fix was the same shape every time: validate the candidate against
> a real object, and record the denominator."

That sentence does three jobs at once. It establishes hands-on depth, it signals
that you think about calibration and trust rather than demos, and it opens the
door to the story in §4.5 — which is the best thing you have.
