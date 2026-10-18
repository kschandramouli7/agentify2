# context-mesh/

The **design-time brain** of agentify — the policies, specs, and decisions that
govern how the product behaves. This is what you + Claude read and refine to build
the product. (The *runtime* mesh — actual pods full of data — lives in the data
layer, described by a self-maintained `pod-registry`, not here.)

## Layout

```
context-mesh/
├── _orchestrator.md      # how query routing & correlation works (the primary pod)
├── policies/             # the 5 "brain" policies — start here
│   ├── storage-strategy.md
│   ├── pod-formation.md
│   ├── refinement-loop.md
│   ├── correlation.md
│   └── data-governance.md
├── specs/                # one feature = one spec  (copy _TEMPLATE.md)
│   ├── _TEMPLATE.md
│   └── 001-event-ingestion.md
├── decisions/            # ADRs, append-only  (copy _TEMPLATE.md)
│   ├── _TEMPLATE.md
│   └── 0001-adopt-context-mesh-architecture.md
└── glossary.md
```

## How to work with this

1. **Refine a policy or write a spec** (English first). Fill in the `_<…>_` prompts.
2. **Hand it to Claude:** _"Implement context-mesh/specs/001-event-ingestion.md."_
3. **Record real tradeoffs** as ADRs as they come up.
4. **Iterate** — the policies are meant to be revised as real data teaches you.

Suggested first pass, in order:
`_orchestrator.md` → `storage-strategy.md` → `pod-formation.md` → `001-event-ingestion.md`.
