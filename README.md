# GoalT (Goal Tree)

A multi-parent, value-propagating goal graph. Concept-stage, open source, looking for people to poke holes in it.

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/furkanYanteri1/GOALT-the-goal-tree/blob/main/demo.ipynb)

## The idea

Most prioritization tools assume a clean hierarchy: one goal breaks into sub-goals, which break into sub-sub-goals, and so on. Real work rarely looks like that. A feature can depend on two other things at once; a bug fix can matter to three different initiatives for three different reasons. Trees don't capture that. A graph might.

GoalT is a small engine for exactly that:

- **One root goal.** Everything traces back to it.
- **Any goal can have multiple children *and* multiple parents.** It's a DAG, not a tree.
- **Every parent distributes exactly 1.0 of value across its children.**
- **A goal with multiple parents accumulates value from each of them** — so goals that genuinely matter to more things naturally float to the top.
- **Adding a goal only recomputes the part of the graph it affects**, not the whole thing.
- **Cycles are rejected explicitly**, not silently allowed to loop.

Value redistribution (how a parent splits its value among children) is pluggable. By default it's a simple equal split — deterministic, no API key needed, always converges. You can swap in an LLM to decide weights based on context instead (e.g. "speed matters more than polish this sprint"). The engine never trusts the LLM's numbers directly: whatever comes back gets validated and re-normalized so the graph stays mathematically consistent even if the model returns something odd.

## Try it without installing anything

Click the "Open in Colab" badge above. It opens `demo.ipynb` in your browser, no setup required. Run the cells top to bottom.

## Local install

```bash
git clone https://github.com/furkanYanteri1/GOALT-the-goal-tree.git
cd GOALT-the-goal-tree
pip install -r requirements.txt
```

```python
from goal_tree import GoalGraph

g = GoalGraph()
g.add_root("root", "Ship v2 of the product")
g.add_goal("a", "Improve onboarding", parents=["root"])
g.add_goal("b", "Improve performance", parents=["root"])
g.add_goal("c", "Fix export bug", parents=["a", "b"])  # depends on both

print(g)
```

```
GoalGraph(root='root')
  1.000  Ship v2 of the product (root)
  1.000  Fix export bug (c)
  0.500  Improve onboarding (a)
  0.500  Improve performance (b)
```

See `demo.ipynb` for the full walkthrough, including the LLM-backed redistribution example.

## Use it inside Claude Code

GoalT ships as a Claude Code plugin: an MCP server (build and query a tree in conversation) plus a live dashboard that auto-starts with the server and highlights, in real time, which goal Claude is currently working on.

### Install as a plugin (recommended)

```
/plugin marketplace add furkanYanteri1/GOALT-the-goal-tree
/plugin install goalt@goalt-marketplace
```

This wires up both the MCP tools and the activity hook automatically. You'll need the plugin's Python dependencies installed once -- Claude Code doesn't manage a venv for plugin MCP servers, so clone the repo and install dependencies before (or right after) installing the plugin:

```bash
git clone https://github.com/furkanYanteri1/GOALT-the-goal-tree.git
cd GOALT-the-goal-tree
pip install -r requirements.txt
```

### Manual install (no activity hook, still works)

```bash
git clone https://github.com/furkanYanteri1/GOALT-the-goal-tree.git
cd GOALT-the-goal-tree
pip install -r requirements.txt
claude mcp add --transport stdio goalt -- python "$(pwd)/mcp_server.py"
```

This gets you the tools but not the automatic "Claude is currently..." activity pulse -- that part relies on the plugin's hook, which is only registered via the plugin install path above.

### Using it

In a Claude Code session:

> "Create a goal tree for shipping v2 of our product, with onboarding and performance as sub-goals, and a shared bug fix that depends on both. Show me the priorities and open the dashboard."

Claude calls `create_tree`, `add_goal`, and `list_priorities`, then `open_dashboard` gives you a URL (`http://127.0.0.1:8765`) to open in your browser. From there:

- **Watch mode**: leave the dashboard open while Claude works. If you installed via the plugin path, the header shows a live "Claude is currently..." indicator on every tool call -- this part is guaranteed, it's wired through a Claude Code hook, not dependent on Claude choosing to report anything.
- **Per-goal highlighting**: when Claude calls `set_active_goal`, the relevant node(s) get a glowing green border in the dashboard. This is best-effort, not guaranteed -- it only happens when Claude chooses to call that tool, which the server instructions ask it to do consistently but can't enforce.
- **Click any node** to see its full description in the side panel.
- **Drag nodes** to rearrange them -- positions stick, they won't snap back on the next update. Click "Reset layout" to let the graph re-lay itself out.

**Available tools:** `create_tree`, `add_goal`, `list_priorities`, `set_active_goal`, `clear_active_goal`, `open_dashboard`, `reset_tree`.

**Current limitation:** state lives in memory for the life of the server process -- it doesn't persist across restarts yet. Saving/loading a tree to disk is a natural next step (see Known open questions).

## What's actually in this repo

- `goal_tree.py` -- the core engine: graph construction, cycle detection, deterministic value propagation, and the pluggable LLM redistribution hook.
- `visualize.py` -- a thin matplotlib/networkx wrapper used by `demo.ipynb` to draw a static graph image.
- `demo.ipynb` -- an interactive, runnable walkthrough (works in Colab, no local setup).
- `mcp_server.py` -- the MCP server: tools for building/querying the tree, plus best-effort active-goal tracking. Starts the dashboard automatically on load.
- `dashboard.py` -- the live, interactive web dashboard (FastAPI + vis-network, single file, no build step), including the hook endpoint Claude Code's PreToolUse hook calls.
- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.mcp.json`, `hooks/hooks.json` -- plugin packaging so the whole thing installs with two commands (see above).

No CLI, no PyPI packaging yet -- natural next steps if there's interest.

## Known open questions

Being upfront about this instead of overselling it:

- **Convergence with LLM-driven weights.** The deterministic fallback always converges by construction. Whether repeated LLM-driven re-weighting stays stable across many edits on a large graph hasn't been proven, only observed on small examples.
- **Global value isn't conserved.** Because a node can have multiple parents, the sum of all node values in the graph is *not* 1.0 overall — only locally, per parent, do children's weights sum to 1.0. That's intentional (it's the mechanism that makes "more real dependencies = more pull" work), but worth understanding before reading too much into raw numbers.
- **Cost at scale.** With a real LLM plugged in, every `add_goal` call can trigger one redistribution call per affected parent. On a large, deep graph that could mean a lot of API calls per edit. Caching / batching isn't implemented yet.
- **No benchmark yet.** This hasn't been compared against classical prioritization methods (AHP, weighted scoring, plain OKR cascading) on a real backlog. That comparison is a natural next step, not a claim already made.
- **Cycle detection is currently a defensive backstop, not an active safeguard.** Through the public `add_goal` API alone, a cycle is impossible by construction (a new node has no outgoing edges yet). The check matters for a planned future feature — linking two already-existing goals together — where cycles become genuinely reachable.
- **MCP server state isn't persisted.** `mcp_server.py` holds one tree in memory for the life of the process; it resets on restart. Save/load to disk is a natural next step.
- **Relationship to existing work.** The value-propagation mechanism is closely related to PageRank-style algorithms on DAGs. If you know prior art that solves this better, please open an issue — genuinely interested, not trying to reinvent something that already exists.

## Contributing

Issues and PRs welcome — see `CONTRIBUTING.md`. Breaking it is as useful as extending it; if you find a case where the graph produces something wrong or unstable, that's exactly the kind of feedback this needs right now.

## License

Apache 2.0 — see `LICENSE`. Use it, fork it, build on it, commercial or not.
