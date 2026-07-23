"""
GoalT MCP server -- exposes the goal_tree engine as tools Claude Code can
call directly in conversation: create a tree, add goals, list rankings,
and mark which goal(s) are being actively worked on right now.

The live dashboard (dashboard.py) starts automatically as soon as this
server starts -- not lazily on first tool call -- because Claude Code's
PreToolUse hook is configured to POST to it on every tool call. If the
dashboard weren't already running, every single tool call would hit a
connection error.

PERSISTENCE: the tree is auto-saved to `<project_root>/.goalt/tree.json`
every time it changes (create_tree, add_goal, link_artifacts), and
auto-loaded from there in two places: once, best-effort, at server startup
(guessing project_root from the process's own working directory), and
reliably via the explicit `load_tree` tool once Claude knows the real
project_root. active_goals / file-edit tracking / uncommitted status are
NOT persisted -- those are meant to reflect "right now", not history.

Run standalone for local testing:
    python mcp_server.py

Add to Claude Code:
    claude mcp add --transport stdio goalt -- python /absolute/path/to/mcp_server.py
"""

from __future__ import annotations

import json
import os

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
        "SESSION START: before assuming no tree exists, call load_tree with "
        "the project's root directory -- a tree from an earlier session may "
        "already be saved on disk. Only fall back to exploring the codebase "
        "and calling create_tree if load_tree reports nothing was found.\n\n"
        "Typical flow otherwise: create_tree once for the top-level "
        "objective, passing project_root (the absolute path to the "
        "project's root directory) so uncommitted-changes tracking and "
        "persistence both work. Then add_goal repeatedly -- a goal can list "
        "more than one parent if it genuinely serves multiple sub-goals, "
        "and can optionally list related_files (paths relative to "
        "project_root) and related_backend (e.g. database tables, edge "
        "functions -- free text, whatever is meaningful for this project). "
        "Use link_artifacts to attach files/backend to a goal after the fact "
        "if you discover them later. Call list_priorities or open_dashboard "
        "to see the result. The tree auto-saves on every change -- no "
        "explicit save step needed.\n\n"
        "ONBOARDING AN EXISTING PROJECT: if the user asks you to build a goal "
        "tree for a codebase that doesn't have one yet (and load_tree found "
        "nothing), explore it first (README, package.json/pyproject.toml, "
        "folder structure, database migrations if present) to understand "
        "its real architecture, then build the tree top-down with "
        "create_tree/add_goal, writing a genuine description for each goal "
        "and linking the files/backend artifacts that actually implement "
        "it. Don't guess at structure without looking -- read enough of the "
        "codebase to get it right.\n\n"
        "KEEPING THE DASHBOARD HONEST: once a goal has related_files linked, "
        "editing one of those files automatically highlights that goal in the "
        "dashboard -- no extra tool call needed, it's driven by a hook. For "
        "goals that don't map cleanly to specific files, you can still call "
        "set_active_goal with a short reason before starting work on them, "
        "and clear_active_goal when done. Do this consistently, not just when "
        "convenient.\n\n"
        "RUNNING SILENTLY IN THE BACKGROUND: once a tree exists for this "
        "project, treat keeping it current as a continuous, unprompted part "
        "of your normal work -- not something the user has to ask for by "
        "name. You will do this without the user ever mentioning 'GoalT' or "
        "using a slash command. Concretely, on every substantive task in this "
        "session (not just ones that mention goals):\n"
        "1. Check whether the task corresponds to an existing goal (glance at "
        "list_priorities or the goal descriptions/related_files you already "
        "know). If it does, call set_active_goal before starting, and "
        "clear_active_goal when done.\n"
        "2. If the task is genuinely new work that doesn't fit any existing "
        "goal, create a new goal for it with add_goal (picking a sensible "
        "parent) as part of your normal workflow -- don't wait to be asked, "
        "and don't ask the user for permission unless it's genuinely unclear "
        "where it belongs. A multi-file feature (e.g. 'let business users "
        "invite a teammate by email') is exactly the kind of new work that "
        "should become a new goal, with related_files added via add_goal or "
        "link_artifacts as you touch each file.\n"
        "3. Do not narrate this to the user (no 'I'm now calling "
        "set_active_goal') -- it should be invisible plumbing. The dashboard "
        "is where they see the effect, not the chat.\n"
        "This is still best-effort, not something the user can fully rely on "
        "-- but it should be the default, not the exception."
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
    "project_root": None,        # absolute path, set by create_tree/load_tree
    "file_edit_goals_raw": {},   # {goal_id: {file, timestamp}} -- guaranteed, hook-driven
    "uncommitted": {},           # {goal_id: [files...]} -- from background git status polling
}

_SAVE_SUBDIR = ".goalt"
_SAVE_FILENAME = "tree.json"


def _save_path(project_root: str) -> str:
    return os.path.join(project_root, _SAVE_SUBDIR, _SAVE_FILENAME)


def _persist(state: dict) -> None:
    """Best-effort save. Never raises -- a disk/permission problem shouldn't break a tool call."""
    graph: GoalGraph | None = state.get("graph")
    project_root = state.get("project_root")
    if graph is None or not project_root:
        return
    try:
        path = _save_path(project_root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(graph.to_dict(), f, indent=2)
    except Exception:
        pass


def _load_from_disk(project_root: str) -> GoalGraph | None:
    """Best-effort load. Returns None (never raises) if nothing valid is found."""
    try:
        path = _save_path(project_root)
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            data = json.load(f)
        return GoalGraph.from_dict(data)
    except Exception:
        return None


# Start the dashboard the moment this module loads, so the hook endpoint is
# always reachable for the whole lifetime of the Claude Code session.
DASHBOARD_URL = start_dashboard_in_background(_state)

# Best-effort auto-load at startup: guess the project root from this
# process's own working directory. This is a guess, not a guarantee -- the
# explicit load_tree tool (with a project_root Claude actually knows) is
# the reliable path and is what the system instructions point Claude to.
_guessed_root = os.getcwd()
_startup_graph = _load_from_disk(_guessed_root)
if _startup_graph is not None:
    _state["graph"] = _startup_graph
    _state["project_root"] = _guessed_root


def _require_graph() -> GoalGraph:
    if _state["graph"] is None:
        raise ValueError("No tree exists yet. Call load_tree first to check for a saved one, or create_tree to start fresh.")
    return _state["graph"]


def _format_ranking(graph: GoalGraph) -> str:
    lines = ["Goals ranked by current value (higher = more priority pull):"]
    for id_, label, value in graph.ranked():
        lines.append(f"  {value:.3f}  {label}  (id: {id_})")
    return "\n".join(lines)


@mcp.tool()
def load_tree(project_root: str) -> str:
    """Load a previously saved tree for this project, if one exists on disk.

    Call this at the start of a session before assuming no tree exists --
    a tree from an earlier session may already be saved. Returns a message
    saying whether anything was found. Does not error if nothing is found;
    just says so, so you can fall back to onboarding/create_tree.

    Args:
        project_root: Absolute path to the project's root directory.
    """
    graph = _load_from_disk(project_root)
    if graph is None:
        return f"No saved tree found at {project_root}. Use create_tree to start one."
    _state["graph"] = graph
    _state["project_root"] = project_root
    _state["active_goals"] = {}
    _state["file_edit_goals_raw"] = {}
    _state["uncommitted"] = {}
    return f"Loaded saved tree for {project_root}. Dashboard: {DASHBOARD_URL}\n\n{_format_ranking(graph)}"


@mcp.tool()
def create_tree(root_label: str, description: str = "", project_root: str = "") -> str:
    """Start a new goal tree with a single root goal. Replaces any existing tree in this session.

    Args:
        root_label: A short description of the top-level objective, e.g. "Ship v2 of the product".
        description: Optional longer explanation, shown when the user clicks this goal in the dashboard.
        project_root: Optional absolute path to the project's root directory. Enables uncommitted-changes
            tracking (via `git status`) and disk persistence once goals have related_files linked. Skip
            only for non-code, throwaway planning you don't need saved.
    """
    graph = GoalGraph()
    graph.add_root("root", root_label, description=description)
    _state["graph"] = graph
    _state["active_goals"] = {}
    _state["file_edit_goals_raw"] = {}
    _state["uncommitted"] = {}
    _state["project_root"] = project_root or None
    _persist(_state)
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
    _persist(_state)
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
    _persist(_state)
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
    """Discard the current tree (in memory and on disk) so a new one can be started with create_tree."""
    project_root = _state.get("project_root")
    if project_root:
        try:
            path = _save_path(project_root)
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
    _state["graph"] = None
    _state["active_goals"] = {}
    _state["file_edit_goals_raw"] = {}
    _state["uncommitted"] = {}
    _state["project_root"] = None
    return "Tree cleared."


if __name__ == "__main__":
    mcp.run(transport="stdio")
