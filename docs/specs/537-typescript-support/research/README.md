# #537: TypeScript support — research notes

Supporting research for adding a TypeScript implementation of Agent Kernel (`ak-ts/`).
Issue: [yaalalabs/agent-kernel#537](https://github.com/yaalalabs/agent-kernel/issues/537).

These are research notes, not requirements. The design spec (`design.md`) will distill
decisions from them and cite them; reviewers should not extract requirements from these files.

All code citations were verified against `develop` @ `7725b1a5` on 2026-07-20.
External numbers (GitHub stars, npm downloads) were observed 2026-07-19/20 and are
order-of-magnitude signals, not precise measurements.

| File | Topic | One-line takeaway |
|---|---|---|
| [ts-framework-landscape.md](ts-framework-landscape.md) | TS/JS agent framework survey | Adapt `@openai/agents` and LangGraph.js first (cleanest `run()`-style APIs, direct symmetry with existing Python adapters); Vercel AI SDK `Agent` and Google ADK JS second; Mastra later; Claude Agent SDK only as a black-box special case. |
| [cross-language-parity-prior-art.md](cross-language-parity-prior-art.md) | How other projects keep Python/TS SDKs in parity | Independent reimplementation governed by written contracts + conformance fixtures + a public parity matrix (LangChain + OpenTelemetry pattern); codegen and shared-native-core approaches don't fit a wrapper library. |
| [portability-inventory.md](portability-inventory.md) | What in ak-py is language-neutral vs Python-coupled | Config format, REST surface, queue contracts, and Terraform are already portable; pickle session serde and Pydantic-as-the-only-config-schema are the two hard blockers to fix first. |
