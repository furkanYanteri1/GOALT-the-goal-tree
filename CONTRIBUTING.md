# Contributing

This is a concept-stage project. The most valuable contributions right now aren't necessarily code — they're honest feedback on whether the core idea holds up.

## Ways to help, roughly in order of how much they're needed right now

1. **Break it.** Build a graph that produces a value distribution that seems wrong, unstable, or surprising. Open an issue with the exact code to reproduce it. This is the single most useful thing right now.
2. **Point to prior art.** If this overlaps with something that already exists and solves it better (PageRank variants, AHP, MCDA methods, DAG-based schedulers), open an issue. Genuinely want to know, not trying to reinvent something that's already solved.
3. **Test convergence at scale.** Build a large graph (50+ nodes, several layers of multi-parent goals) and see whether repeated edits stay stable. Report what you find, code or no code.
4. **Try a real LLM in the redistribution hook.** `goal_tree.make_llm_redistribution_fn` is a template — wire it up to a real model, share what worked and what didn't (prompt design, temperature, failure modes).
5. **Improve the deterministic fallback.** The current default is a flat equal-split. A proper PageRank-style weighted propagation (using edge metadata like effort, urgency, etc.) would be a meaningful upgrade and doesn't require any LLM.
6. **Docs, examples, small fixes.** Always welcome, just less urgent than the above right now.

## Ground rules

- Keep PRs small and focused — one idea per PR is easier to review and merge.
- If you're proposing a structural change (e.g. changing how value propagation works), open an issue first to discuss before writing code. Saves everyone time.
- Be direct in code review. This is early enough that blunt technical disagreement is more useful than politeness.

## Setup

```bash
git clone https://github.com/furkanYanteri1/GOALT-the-goal-tree.git
cd GOALT-the-goal-tree
pip install -r requirements.txt
python -m pytest  # if/when tests exist
```

No CLA, no formal process. Open an issue or a PR and it'll get read.
