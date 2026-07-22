"""
GoalT MCP server -- exposes the goal_tree engine as tools Claude Code can
call directly in conversation: create a tree, add goals, list rankings,
and open a live, interactive dashboard (drag/zoom/hover) in the browser.

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
        "matters most given real dependencies between them, or see the "
        "resulting priority graph visually. Typical flow: create_tree once "
        "for the top-level objective, then add_goal repeatedly (a goal can "
        "list more than one parent if it genuinely serves multiple sub-goals), "
        "then list_priorities or open_dashboard to see the result. The "
        "dashboard is a live, interactive page that updates automatically as "
        "goals are added -- open it once and leave it open."
    ),
)

# Single in-memory tree for the life of this server process, shared with the
# dashboard's HTTP handlers so both see the same live state.
_state: dict = {"graph": None, "_dashboard_url": None}


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
def create_tree(root_label: str) -> str:
    """Start a new goal tree with a single root goal. Replaces any existing tree in this session.

    Args:
        root_label: A short description of the top-level objective, e.g. "Ship v2 of the product".
    """
    graph = GoalGraph()
    graph.add_root("root", root_label)
    _state["graph"] = graph
    return f"Created tree with root: '{root_label}' (id: root)"


@mcp.tool()
def add_goal(id: str, label: str, parents: list[str]) -> str:
    """Add a goal to the current tree under one or more existing parent goals.

    A goal can have more than one parent if it genuinely serves multiple
    higher-level goals -- it will accumulate value from each parent, which
    is how goals with real cross-cutting importance naturally rank higher.

    Args:
        id: A short unique identifier for this goal, e.g. "fix_export_bug".
        label: A human-readable description of the goal.
        parents: List of existing goal ids this goal belongs under. Must include "root" or another already-added goal id.
    """
    graph = _require_graph()
    try:
        graph.add_goal(id, label, parents=parents)
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
def open_dashboard() -> str:
    """Start (if not already running) and return the URL of a live, interactive dashboard for the current tree.

    The dashboard is a draggable, zoomable graph that auto-refreshes as
    goals are added -- open it once in a browser and it stays in sync.
    """
    url = start_dashboard_in_background(_state)
    return f"Dashboard running at {url} -- open it in a browser. It updates automatically as the tree changes, no need to reopen it after adding more goals."


@mcp.tool()
def reset_tree() -> str:
    """Discard the current tree so a new one can be started with create_tree."""
    _state["graph"] = None
    return "Tree cleared."


if __name__ == "__main__":
    mcp.run(transport="stdio")
