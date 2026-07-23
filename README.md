This wires up both the MCP tools and the activity hook automatically. You'll need the plugin's Python dependencies installed once -- Claude Code doesn't manage a venv for plugin MCP servers, so clone the repo and install dependencies before (or right after) installing the plugin:

```bash
git clone https://github.com/furkanYanteri1/GOALT-the-goal-tree.git
cd GOALT-the-goal-tree
python3 -m pip install -r requirements.txt
```

### Manual install (no activity hook, still works)

```bash
git clone https://github.com/furkanYanteri1/GOALT-the-goal-tree.git
cd GOALT-the-goal-tree
python3 -m pip install -r requirements.txt
claude mcp add --transport stdio goalt -- python "$(pwd)/mcp_server.py"
```

This gets you the tools but not the automatic "Claude is currently..." activity pulse -- that part relies on the plugin's hook, which is only registered via the plugin install path above.

### Using it

**Onboarding an existing codebase:** run the `/goalt:start` slash command in the project you want to plan. Claude explores the repo (README, package manifest, folder structure, database migrations if present), identifies real functional areas, and builds a tree with genuine descriptions -- linking the files and backend artifacts (database tables, edge functions, etc.) it's confident actually implement each goal, rather than fabricating structure.

**Starting from scratch,** in a Claude Code session:

> "Create a goal tree for shipping v2 of our product, with onboarding and performance as sub-goals, and a shared bug fix that depends on both. Show me the priorities and open the dashboard."

Claude calls `create_tree`, `add_goal`, and `list_priorities`, then `open_dashboard` gives you a URL (`http://127.0.0.1:8765`) to open in your browser. From there:

- **Watch mode**: leave the dashboard open while Claude works. The header shows a live "Claude is currently..." indicator on every tool call -- guaranteed, wired through a Claude Code hook.
- **File-edit highlighting (guaranteed)**: once a goal has `related_files` linked, editing one of those files automatically highlights that goal with a glowing green border -- no extra tool call needed, the hook matches the edited path against every goal's related files.
- **Self-reported highlighting (best-effort)**: for goals that don't map cleanly to specific files, Claude can call `set_active_goal` with a reason. Best-effort only -- it happens if Claude chooses to call it, not guaranteed.
- **Uncommitted-changes tracking**: if `project_root` was set when the tree was created, a background thread polls `git status` every few seconds and marks goals with an amber border if any of their related files have uncommitted changes -- independent of whether anything is being actively edited right now.
- **Click any node** to open a side panel with two tabs: **Description** (the goal's description plus its related files/backend artifacts) and **Changes** (files currently being edited and/or with uncommitted changes, for this specific goal). Clicking a node that's currently active or has uncommitted work opens straight to the Changes tab.
- **Drag nodes** to rearrange them -- positions stick, they won't snap back on the next update. Click "Reset layout" to let the graph re-lay itself out.

**Available tools:** `create_tree`, `add_goal`, `link_artifacts`, `list_priorities`, `set_active_goal`, `clear_active_goal`, `open_dashboard`, `reset_tree`.

**Current limitations:**
- State lives in memory for the life of the server process -- it doesn't persist across restarts yet. Saving/loading a tree to disk is a natural next step (see Known open questions).
- File-to-goal matching (`goals_for_file`) is a heuristic suffix match, not exact-path resolution -- it can mismatch on ambiguous relative paths in unusual project layouts.
- Uncommitted-changes tracking assumes a single git repository at `project_root` and re-polls on a fixed interval (a few seconds), so there's a small lag between a change happening and it showing up.

## What's actually in this repo

- `goal_tree.py` -- the core engine: graph construction, cycle detection, deterministic value propagation, and the pluggable LLM redistribution hook.
- `visualize.py` -- a thin matplotlib/networkx wrapper used by `demo.ipynb` to draw a static graph image.
- `demo.ipynb` -- an interactive, runnable walkthrough (works in Colab, no local setup).
- `mcp_server.py` -- the MCP server: tools for building/querying the tree, plus best-effort active-goal tracking. Starts the dashboard automatically on load.
- `dashboard.py` -- the live, interactive web dashboard (FastAPI + vis-network, single file, no build step), including the hook endpoint Claude Code's PreToolUse hook calls.
- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.mcp.json`, `hooks/hooks.json` -- plugin packaging so the whole thing installs with two commands (see above).
- `commands/start.md` -- the `/goalt:start` slash command that onboards GoalT onto an existing codebase.

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