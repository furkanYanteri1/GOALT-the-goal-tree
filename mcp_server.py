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
        "Typical flow: create_tree once for the top-level objective, passing "
        "project_root (the absolute path to the project's root directory) so "
        "uncommitted-changes tracking works. Then add_goal repeatedly -- a "
        "goal can list more than one parent if it genuinely serves multiple "
        "sub-goals, and can optionally list related_files (paths relative to "
        "project_root) and related_backend (e.g. database tables, edge "
        "functions -- free text, whatever is meaningful for this project). "
        "Use link_artifacts to attach files/backend to a goal after the fact "
        "if you discover them later. Call list_priorities or open_dashboard "
        "to see the result.\n\n"
        "ONBOARDING AN EXISTING PROJECT: if the user asks you to build a goal "
        "tree for a codebase that doesn't have one yet, explore it first "
        "(README, package.json/pyproject.toml, folder structure, database "
        "migrations if present) to understand its real architecture, then "
        "build the tree top-down with create_tree/add_goal, writing a genuine "
        "description for each goal and linking the files/backend artifacts "
        "that actually implement it. Don't guess at structure without looking "
        "-- read enough of the codebase to get it right.\n\n"
        "KEEPING THE DASHBOARD HONEST: once a goal has related_files linked, "
        "editing one of those files automatically highlights that goal in the "
        "dashboard -- no extra tool call needed, it's driven by a hook. For "
        "goals that don't map cleanly to specific files, you can still call "
        "set_active_goal with a short reason before starting work on them, "
        "and clear_active_goal when done. Do this consistently, not just when "
        "convenient."
    ),
)

# Single in-memory tree for the life of this server process, shared with the
# dashboard's HTTP handlers (including the hook endpoints) so everything
# sees the same live state.
_state: dict = {
    "graph": None,
    "_dashboard_url": None,
    "active_goals": {},          # {goal_id: reason} -- best-effort, set by set_active_goal
    "last_activity": None,       # {tool_name, description, timestamp} -- from PreToolUse hook
    "project_root": None,        # absolute path, set by create_tree, used for git status polling
    "file_edit_goals_raw": {},   # {goal_id: {file, timestamp}} -- guaranteed, hook-driven
    "uncommitted": {},           # {goal_id: [files...]} -- from background git status polling
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
def create_tree(root_label: str, description: str = "", project_root: str = "") -> str:
    """Start a new goal tree with a single root goal. Replaces any existing tree in this session.

    Args:
        root_label: A short description of the top-level objective, e.g. "Ship v2 of the product".
        description: Optional longer explanation, shown when the user clicks this goal in the dashboard.
        project_root: Optional absolute path to the project's root directory. Enables uncommitted-changes
            tracking (via `git status`) once goals have related_files linked. Skip for non-code planning.
    """
    graph = GoalGraph()
    graph.add_root("root", root_label, description=description)
    _state["graph"] = graph
    _state["active_goals"] = {}
    _state["file_edit_goals_raw"] = {}
    _state["uncommitted"] = {}
    _state["project_root"] = project_root or None
    return f"Created tree with root: '{root_label}' (id: root). Dashboard: {DASHBOARD_URL}"


@mcp.tool()
def add_goal(
    id: str,
    label: str,
    parents: list[str],
    description: str = "",
    related_files: list[str] | None = None,
    related_backend: list[str] | None = None,
) -> str:
    """Add a goal to the current tree under one or more existing parent goals.

    A goal can have more than one parent if it genuinely serves multiple
    higher-level goals -- it will accumulate value from each parent, which
    is how goals with real cross-cutting importance naturally rank higher.

    Args:
        id: A short unique identifier for this goal, e.g. "fix_export_bug".
        label: A human-readable description of the goal.
        parents: List of existing goal ids this goal belongs under. Must include "root" or another already-added goal id.
        description: Optional longer explanation, shown when the user clicks this goal in the dashboard.
        related_files: Optional file paths (relative to project_root) that implement this goal. Editing one of
            these files later automatically highlights this goal in the dashboard.
        related_backend: Optional free-text backend artifacts relevant to this goal, e.g. "supabase.orders table"
            or "edge function: create-payment-intent".
    """
    graph = _require_graph()
    try:
        graph.add_goal(
            id, label, parents=parents, description=description,
            related_files=related_files, related_backend=related_backend,
        )
    except CycleError as e:
        return f"Rejected: {e}"
    except (ValueError, WeightError) as e:
        return f"Error: {e}"
    return f"Added '{label}' (id: {id}) under {parents}.\n\n{_format_ranking(graph)}"


@mcp.tool()
def link_artifacts(id: str, related_files: list[str] | None = None, related_backend: list[str] | None = None) -> str:
    """Attach additional related files / backend artifacts to an existing goal.

    Appends to whatever's already linked (deduplicated) -- use this when you
    discover more relevant files/artifacts for a goal after it was created.

    Args:
        id: Existing goal id.
        related_files: File paths (relative to project_root) to add.
        related_backend: Free-text backend artifacts to add.
    """
    graph = _require_graph()
    try:
        graph.link_artifacts(id, files=related_files, backend=related_backend)
    except ValueError as e:
        return f"Error: {e}"
    return f"Linked artifacts to '{id}'."


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
    _state["file_edit_goals_raw"] = {}
    _state["uncommitted"] = {}
    _state["project_root"] = None
    return "Tree cleared."


if __name__ == "__main__":
    mcp.run(transport="stdio")
