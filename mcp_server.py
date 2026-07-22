"""
GoalT MCP server -- exposes the goal_tree engine as tools Claude Code can
call directly in conversation: create a tree, add goals, list rankings,
and mark which goal(s) are being actively worked on right now.

The live dashboard (dashboard.py) starts automatically as soon as this
server starts -- not lazily on first tool call -- because Claude Code's
PreToolUse hook is configured to POST to it on every tool call. If the
dashboard weren't already running, every single tool call would hit a
connection error.

Run standalone for local testing:
    python mcp_server.py

Add to Claude Code:
    claude mcp add --transport stdio goalt -- python /absolute/path/to/mcp_server.py

State is held in memory for the life of the server process (one tree per
running server). It does not persist across restarts yet -- see the
"Known open questions" section in README.md.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from goal_tree import GoalGraph, CycleError, WeightError
from dashboard import start_dashboard_in_background


mcp = FastMCP(
    name="goalt",
    instructions=(
        "Tools for building and querying a GoalT goal tree: a multi-parent, "
        "value-propagating goal graph. Use these when the user wants to break "
        "down a project or objective into sub-goals, understand which sub-goal "
        "matters most given real dependencies between them, or track work "
        "against a live visual plan.\n\n"
        "Typical flow: create_tree once for the top-level objective (a "
        "description is optional but makes the dashboard more useful), then "
        "add_goal repeatedly -- a goal can list more than one parent if it "
        "genuinely serves multiple sub-goals. Call list_priorities or "
        "open_dashboard to see the result.\n\n"
        "IMPORTANT -- keeping the dashboard honest: whenever you are about to "
        "do substantive work that clearly corresponds to one or more goals in "
        "the tree, call set_active_goal with those goal ids and a short reason "
        "first. This highlights them live in the dashboard so the user can see "
        "what you're working on and why. Call clear_active_goal when that unit "
        "of work is done. This is a courtesy to the user watching the "
        "dashboard, not a hard requirement -- but do it consistently, don't "
        "skip it for convenience."
    ),
)

# Single in-memory tree for the life of this server process, shared with the
# dashboard's HTTP handlers (including the hook endpoints) so everything
# sees the same live state.
_state: dict = {
    "graph": None,
    "_dashboard_url": None,
    "active_goals": {},   # {goal_id: reason} -- best-effort, set by set_active_goal
    "last_activity": None,  # {tool_name, description, timestamp} -- from PreToolUse hook
}

# Start the dashboard the moment this module loads, so the hook endpoint is
# always reachable for the whole lifetime of the Claude Code session.
DASHBOARD_URL = start_dashboard_in_background(_state)


def _require_graph() -> GoalGraph:
    if _state["graph"] is None:
        raise ValueError("No tree exists yet. Call create_tree first.")
    return _state["graph"]


def _format_ranking(graph: GoalGraph) -> str:
    lines = ["Goals ranked by current value (higher = more priority pull):"]
    for id_, label, value in graph.ranked():
        lines.append(f"  {value:.3f}  {label}  (id: {id_})")
    return "\n".join(lines)


@mcp.tool()
def create_tree(root_label: str, description: str = "") -> str:
    """Start a new goal tree with a single root goal. Replaces any existing tree in this session.

    Args:
        root_label: A short description of the top-level objective, e.g. "Ship v2 of the product".
        description: Optional longer explanation, shown when the user clicks this goal in the dashboard.
    """
    graph = GoalGraph()
    graph.add_root("root", root_label, description=description)
    _state["graph"] = graph
    _state["active_goals"] = {}
    return f"Created tree with root: '{root_label}' (id: root). Dashboard: {DASHBOARD_URL}"


@mcp.tool()
def add_goal(id: str, label: str, parents: list[str], description: str = "") -> str:
    """Add a goal to the current tree under one or more existing parent goals.

    A goal can have more than one parent if it genuinely serves multiple
    higher-level goals -- it will accumulate value from each parent, which
    is how goals with real cross-cutting importance naturally rank higher.

    Args:
        id: A short unique identifier for this goal, e.g. "fix_export_bug".
        label: A human-readable description of the goal.
        parents: List of existing goal ids this goal belongs under. Must include "root" or another already-added goal id.
        description: Optional longer explanation, shown when the user clicks this goal in the dashboard.
    """
    graph = _require_graph()
    try:
        graph.add_goal(id, label, parents=parents, description=description)
    except CycleError as e:
        return f"Rejected: {e}"
    except (ValueError, WeightError) as e:
        return f"Error: {e}"
    return f"Added '{label}' (id: {id}) under {parents}.\n\n{_format_ranking(graph)}"


@mcp.tool()
def list_priorities() -> str:
    """Return every goal in the current tree, ranked by its current computed value."""
    graph = _require_graph()
    return _format_ranking(graph)


@mcp.tool()
def set_active_goal(ids: list[str], reason: str) -> str:
    """Mark one or more goals as being actively worked on right now -- highlights them live in the dashboard.

    Call this right before starting substantive work that clearly
    corresponds to a goal in the tree. Replaces whatever was previously
    marked active. Call clear_active_goal when this unit of work is done.

    Args:
        ids: Goal ids currently being worked on.
        reason: A short, user-facing explanation of what you're doing and why it relates to these goals.
    """
    graph = _require_graph()
    unknown = [i for i in ids if i not in graph.g.nodes]
    if unknown:
        return f"Error: unknown goal id(s): {unknown}"
    _state["active_goals"] = {i: reason for i in ids}
    return f"Marked active: {ids} -- {reason}"


@mcp.tool()
def clear_active_goal() -> str:
    """Clear whichever goals were marked active, e.g. once the current unit of work is finished."""
    _state["active_goals"] = {}
    return "Cleared active goals."


@mcp.tool()
def open_dashboard() -> str:
    """Return the URL of the live, interactive dashboard for the current tree.

    The dashboard starts automatically with this server -- this just gives
    you the URL to hand to the user. It's a draggable, zoomable graph that
    auto-refreshes as goals are added or marked active, and shows a live
    "Claude is currently..." indicator driven by Claude Code's own hooks.
    """
    return f"Dashboard running at {DASHBOARD_URL} -- open it in a browser. It updates automatically, no need to reopen it."


@mcp.tool()
def reset_tree() -> str:
    """Discard the current tree so a new one can be started with create_tree."""
    _state["graph"] = None
    _state["active_goals"] = {}
    return "Tree cleared."


if __name__ == "__main__":
    mcp.run(transport="stdio")
