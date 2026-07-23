"""
Local, interactive dashboard for GoalT. Serves a single HTML page with a
draggable/zoomable graph (vis-network, loaded from a CDN in the browser)
that polls the running MCP server's in-memory graph and re-renders on
change -- without discarding node positions you've dragged.

Three ways a goal can show as "active", with different guarantees:

1. Generic activity pulse (guaranteed): Claude Code hooks (PreToolUse) POST
   to /hooks/pre-tool-use on every tool call, regardless of what it's doing.
   This always fires. It doesn't know which goal is involved, just that
   *something* is happening.

2. File-edit-driven highlighting (guaranteed, once a goal has related_files
   linked): when the hook sees an Edit/Write call, it checks the file path
   against every goal's related_files (via GoalGraph.goals_for_file) and
   marks matching goals active automatically. No LLM cooperation required --
   this is deterministic string matching, not a tool Claude has to remember
   to call.

3. Self-reported highlighting (best-effort): the MCP server's set_active_goal
   tool marks specific goal ids as "being worked on right now". This only
   happens if Claude chooses to call it. Useful for goals that don't map
   cleanly to specific files.

Uncommitted-changes tracking is a fourth, separate signal: a background
thread periodically runs `git status --porcelain` in the project root (set
via create_tree's project_root argument) and maps changed files to goals the
same way. This persists even when nothing is actively being edited right
now -- it answers "what have I touched but not committed", not "what's
happening this second".

The dashboard runs on a fixed port (see PORT below) rather than scanning for
a free one, because the hook URL Claude Code is configured to call is a
static string in plugin.json -- it has to know where to find us in advance.
"""

from __future__ import annotations

import subprocess
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from goal_tree import GoalGraph

PORT = 8765

# How long a file-edit-driven highlight stays "active" after the last edit
# to that file, in seconds. Keeps the highlight from flickering off between
# consecutive edits but also from staying lit forever after work moves on.
FILE_EDIT_ACTIVE_WINDOW = 20

# How often the background thread re-checks `git status` for uncommitted
# changes, in seconds.
GIT_POLL_INTERVAL = 4


PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>GoalT</title>
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, Segoe UI, sans-serif; background: #111318; color: #e8e8e8; }
    #header { padding: 12px 20px; border-bottom: 1px solid #2a2d35; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    #header h1 { font-size: 16px; margin: 0; font-weight: 600; }
    #activity { font-size: 12px; color: #9aa0a8; display: flex; align-items: center; gap: 6px; flex: 1; justify-content: center; }
    #pulse { width: 8px; height: 8px; border-radius: 50%; background: #4a4d55; transition: background 0.3s; }
    #pulse.live { background: #4ade80; box-shadow: 0 0 8px #4ade80; }
    #resetBtn { background: #1e2128; border: 1px solid #333640; color: #cfd3da; padding: 6px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; }
    #resetBtn:hover { background: #262932; }
    #main { display: flex; height: calc(100vh - 46px); }
    #network { flex: 1; }
    #panel { width: 320px; border-left: 1px solid #2a2d35; padding: 0; overflow-y: auto; display: none; }
    #panel.open { display: block; }
    #panelHead { padding: 16px 16px 0; }
    #panel h2 { font-size: 15px; margin: 0 0 4px; }
    #panel .value { font-size: 12px; color: #9aa0a8; margin-bottom: 12px; }
    #tabs { display: flex; border-bottom: 1px solid #2a2d35; margin-top: 8px; }
    .tabBtn { flex: 1; background: none; border: none; color: #9aa0a8; padding: 10px 0; font-size: 12px; cursor: pointer; border-bottom: 2px solid transparent; }
    .tabBtn.active { color: #e8e8e8; border-bottom-color: #4ade80; }
    #tabBody { padding: 16px; }
    .desc { font-size: 13px; line-height: 1.5; color: #cfd3da; white-space: pre-wrap; margin-bottom: 16px; }
    .empty { font-size: 13px; color: #6b7078; font-style: italic; }
    .artifactGroup { margin-bottom: 14px; }
    .artifactGroup h3 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: #7d8590; margin: 0 0 6px; }
    .artifactGroup ul { margin: 0; padding-left: 18px; font-size: 12px; color: #cfd3da; line-height: 1.6; }
    .changeRow { font-size: 12px; color: #cfd3da; padding: 6px 0; border-bottom: 1px solid #1e2128; }
    .changeRow .badge { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 10px; margin-right: 6px; }
    .badge.editing { background: rgba(74, 222, 128, 0.15); color: #4ade80; }
    .badge.uncommitted { background: rgba(245, 158, 11, 0.15); color: #f59e0b; }
    #empty { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: #7d8590; text-align: center; }
  </style>
</head>
<body>
  <div id="header">
    <h1>GoalT</h1>
    <div id="activity"><span id="pulse"></span><span id="activityText">waiting for Claude Code...</span></div>
    <button id="resetBtn" title="Snap all nodes back to their computed layout">Reset layout</button>
  </div>
  <div id="main">
    <div id="network"></div>
    <div id="panel">
      <div id="panelHead">
        <h2 id="panelTitle"></h2>
        <div class="value" id="panelValue"></div>
        <div id="tabs">
          <button class="tabBtn" data-tab="description">Description</button>
          <button class="tabBtn" data-tab="changes">Changes</button>
        </div>
      </div>
      <div id="tabBody"></div>
    </div>
  </div>
  <div id="empty" style="display:none">No tree yet. Ask Claude to create one.</div>

  <script>
    const container = document.getElementById('network');
    const nodesDS = new vis.DataSet([]);
    const edgesDS = new vis.DataSet([]);
    const network = new vis.Network(container, { nodes: nodesDS, edges: edgesDS }, {
      nodes: { shape: 'dot', font: { color: '#e8e8e8', size: 13 }, borderWidth: 2 },
      edges: {
        arrows: 'to', color: { color: '#4a4d55', highlight: '#8b8f99' },
        font: { color: '#7d8590', size: 10, strokeWidth: 0 },
        smooth: { type: 'cubicBezier', roundness: 0.4 },
      },
      physics: { stabilization: true, barnesHut: { gravitationalConstant: -4000, springLength: 140 } },
      interaction: { hover: true, zoomView: true, dragView: true },
    });

    network.on('dragEnd', (params) => {
      params.nodes.forEach(id => nodesDS.update({ id, fixed: { x: true, y: true } }));
    });

    document.getElementById('resetBtn').onclick = () => {
      const ids = nodesDS.getIds();
      nodesDS.update(ids.map(id => ({ id, fixed: false })));
      network.setOptions({ physics: true });
      network.stabilize();
    };

    const panel = document.getElementById('panel');
    let currentTab = 'description';
    let currentNode = null;

    function renderTabBody() {
      if (!currentNode) return;
      const body = document.getElementById('tabBody');
      document.querySelectorAll('.tabBtn').forEach(b => b.classList.toggle('active', b.dataset.tab === currentTab));

      if (currentTab === 'description') {
        let html = '';
        if (currentNode.description) {
          html += `<div class="desc">${escapeHtml(currentNode.description)}</div>`;
        } else {
          html += `<div class="desc empty">No description set for this goal.</div>`;
        }
        if (currentNode.related_files && currentNode.related_files.length) {
          html += `<div class="artifactGroup"><h3>Related files</h3><ul>${currentNode.related_files.map(f => `<li>${escapeHtml(f)}</li>`).join('')}</ul></div>`;
        }
        if (currentNode.related_backend && currentNode.related_backend.length) {
          html += `<div class="artifactGroup"><h3>Backend</h3><ul>${currentNode.related_backend.map(f => `<li>${escapeHtml(f)}</li>`).join('')}</ul></div>`;
        }
        body.innerHTML = html;
      } else {
        let html = '';
        if (currentNode.editingFiles && currentNode.editingFiles.length) {
          html += currentNode.editingFiles.map(f => `<div class="changeRow"><span class="badge editing">editing</span>${escapeHtml(f)}</div>`).join('');
        }
        if (currentNode.uncommittedFiles && currentNode.uncommittedFiles.length) {
          html += currentNode.uncommittedFiles.map(f => `<div class="changeRow"><span class="badge uncommitted">uncommitted</span>${escapeHtml(f)}</div>`).join('');
        }
        if (!html) html = '<div class="empty">No active edits or uncommitted changes detected for this goal.</div>';
        body.innerHTML = html;
      }
    }

    function escapeHtml(s) {
      const d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }

    document.querySelectorAll('.tabBtn').forEach(btn => {
      btn.onclick = () => { currentTab = btn.dataset.tab; renderTabBody(); };
    });

    network.on('click', (params) => {
      if (!params.nodes.length) { panel.classList.remove('open'); currentNode = null; return; }
      const node = nodesDS.get(params.nodes[0]);
      currentNode = node;
      document.getElementById('panelTitle').textContent = node.rawLabel;
      document.getElementById('panelValue').textContent = 'value: ' + node.value.toFixed(3);
      // Auto-open the Changes tab if this node currently has activity or
      // uncommitted work -- that's almost certainly what you clicked for.
      const hasActivity = (node.editingFiles && node.editingFiles.length) || (node.uncommittedFiles && node.uncommittedFiles.length) || node.selfActive;
      currentTab = hasActivity ? 'changes' : 'description';
      renderTabBody();
      panel.classList.add('open');
    });

    let lastSignature = null;

    async function pollGraph() {
      try {
        const res = await fetch('/api/graph');
        const data = await res.json();

        const emptyEl = document.getElementById('empty');
        if (!data.nodes.length) {
          emptyEl.style.display = 'block';
          nodesDS.clear(); edgesDS.clear();
          lastSignature = null;
          return;
        }
        emptyEl.style.display = 'none';

        const signature = JSON.stringify(data);
        if (signature === lastSignature) return;
        lastSignature = signature;

        const maxVal = Math.max(...data.nodes.map(n => n.value), 0.001);
        const selfActiveIds = new Set(Object.keys(data.active_goals || {}));
        const fileEditMap = data.file_edit_goals || {};   // {goal_id: [files...]}
        const uncommittedMap = data.uncommitted || {};      // {goal_id: [files...]}

        const existingIds = new Set(nodesDS.getIds());
        const incomingIds = new Set(data.nodes.map(n => n.id));

        const visNodes = data.nodes.map(n => {
          const selfActive = selfActiveIds.has(n.id);
          const editingFiles = fileEditMap[n.id] || [];
          const uncommittedFiles = uncommittedMap[n.id] || [];
          const isActive = selfActive || editingFiles.length > 0;
          const hasUncommitted = uncommittedFiles.length > 0;

          let borderColor = '#e8e8e8', borderWidth = 2, shadow = { enabled: false };
          if (isActive) {
            borderColor = '#4ade80'; borderWidth = 4;
            shadow = { enabled: true, color: 'rgba(74, 222, 128, 0.6)', size: 20, x: 0, y: 0 };
          } else if (hasUncommitted) {
            borderColor = '#f59e0b'; borderWidth = 3;
            shadow = { enabled: true, color: 'rgba(245, 158, 11, 0.45)', size: 14, x: 0, y: 0 };
          }

          return {
            id: n.id,
            rawLabel: n.label,
            description: n.description || '',
            related_files: n.related_files || [],
            related_backend: n.related_backend || [],
            editingFiles, uncommittedFiles, selfActive,
            value: n.value,
            label: `${n.label}\\n${n.value.toFixed(2)}`,
            size: 14 + 26 * (n.value / maxVal),
            color: { background: shade(n.value / maxVal), border: borderColor },
            borderWidth, shadow,
          };
        });

        nodesDS.update(visNodes);
        for (const id of existingIds) {
          if (!incomingIds.has(id)) nodesDS.remove(id);
        }

        const visEdges = data.edges.map(e => ({
          id: e.from + '->' + e.to, from: e.from, to: e.to,
          label: e.weight !== null ? e.weight.toFixed(2) : '',
        }));
        edgesDS.update(visEdges);
        const existingEdgeIds = new Set(edgesDS.getIds());
        const incomingEdgeIds = new Set(visEdges.map(e => e.id));
        for (const id of existingEdgeIds) {
          if (!incomingEdgeIds.has(id)) edgesDS.remove(id);
        }

        if (currentNode) {
          const refreshed = nodesDS.get(currentNode.id);
          if (refreshed) { currentNode = refreshed; renderTabBody(); }
        }
      } catch (err) {
        document.getElementById('activityText').textContent = 'disconnected -- is the MCP server still running?';
      }
    }

    async function pollActivity() {
      try {
        const res = await fetch('/api/activity');
        const data = await res.json();
        const pulse = document.getElementById('pulse');
        const text = document.getElementById('activityText');
        if (data.last_activity && (Date.now() / 1000 - data.last_activity.timestamp) < 6) {
          pulse.classList.add('live');
          text.textContent = data.last_activity.description || ('Claude is running: ' + data.last_activity.tool_name);
        } else {
          pulse.classList.remove('live');
          text.textContent = 'idle';
        }
      } catch (err) { /* keep last known state */ }
    }

    function shade(t) {
      const r = Math.round(60 + t * 195);
      const g = Math.round(90 + t * 100);
      const b = Math.round(160 - t * 100);
      return `rgb(${r}, ${g}, ${b})`;
    }

    pollGraph();
    pollActivity();
    setInterval(pollGraph, 1500);
    setInterval(pollActivity, 1500);
  </script>
</body>
</html>
"""


def _describe_tool_call(tool_name: str, tool_input: dict) -> str:
    """Best-effort human-readable summary of a hook payload for the activity pulse."""
    if tool_name in ("Edit", "Write") and isinstance(tool_input, dict) and tool_input.get("file_path"):
        return f"Editing {tool_input['file_path']}"
    if tool_name == "Bash" and isinstance(tool_input, dict) and tool_input.get("command"):
        cmd = tool_input["command"]
        return f"Running: {cmd[:60]}{'...' if len(cmd) > 60 else ''}"
    if tool_name == "Read" and isinstance(tool_input, dict) and tool_input.get("file_path"):
        return f"Reading {tool_input['file_path']}"
    return f"Running: {tool_name}"


def _prune_stale_file_edits(state: dict) -> None:
    now = time.time()
    fe = state.get("file_edit_goals_raw", {})
    state["file_edit_goals_raw"] = {
        goal_id: entry for goal_id, entry in fe.items()
        if now - entry["timestamp"] < FILE_EDIT_ACTIVE_WINDOW
    }


def _git_uncommitted_files(project_root: str) -> list[str]:
    """Return paths (relative to project_root) with uncommitted changes, or [] on any failure."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        files = []
        for line in result.stdout.splitlines():
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ")[-1].strip()
            if path:
                files.append(path)
        return files
    except Exception:
        return []


def _git_polling_loop(state: dict) -> None:
    while True:
        project_root = state.get("project_root")
        graph: GoalGraph | None = state.get("graph")
        if project_root and graph is not None:
            changed_files = _git_uncommitted_files(project_root)
            mapping: dict[str, list[str]] = {}
            for f in changed_files:
                for goal_id in graph.goals_for_file(f):
                    mapping.setdefault(goal_id, []).append(f)
            state["uncommitted"] = mapping
        else:
            state["uncommitted"] = {}
        time.sleep(GIT_POLL_INTERVAL)


def create_app(state: dict) -> FastAPI:
    """state is the same dict the MCP server holds its GoalGraph in."""
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/api/graph")
    def api_graph():
        graph: GoalGraph | None = state.get("graph")
        if graph is None:
            return JSONResponse({"nodes": [], "edges": [], "active_goals": {}, "file_edit_goals": {}, "uncommitted": {}})
        payload = graph.to_dict()
        payload["active_goals"] = state.get("active_goals", {})

        _prune_stale_file_edits(state)
        file_edit_map: dict[str, list[str]] = {}
        for goal_id, entry in state.get("file_edit_goals_raw", {}).items():
            file_edit_map.setdefault(goal_id, []).append(entry["file"])
        payload["file_edit_goals"] = file_edit_map
        payload["uncommitted"] = state.get("uncommitted", {})
        return JSONResponse(payload)

    @app.get("/api/activity")
    def api_activity():
        return JSONResponse({"last_activity": state.get("last_activity")})

    @app.post("/hooks/pre-tool-use")
    async def hook_pre_tool_use(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        tool_name = payload.get("tool_name", "unknown")
        tool_input = payload.get("tool_input", {}) or {}
        state["last_activity"] = {
            "tool_name": tool_name,
            "description": _describe_tool_call(tool_name, tool_input),
            "timestamp": time.time(),
        }

        if tool_name in ("Edit", "Write") and tool_input.get("file_path"):
            graph: GoalGraph | None = state.get("graph")
            if graph is not None:
                file_path = tool_input["file_path"]
                matched_goal_ids = graph.goals_for_file(file_path)
                if matched_goal_ids:
                    fe = state.setdefault("file_edit_goals_raw", {})
                    for goal_id in matched_goal_ids:
                        fe[goal_id] = {"file": file_path, "timestamp": time.time()}

        return JSONResponse({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        })

    return app


def start_dashboard_in_background(state: dict) -> str:
    """Starts the dashboard (and the git-status polling thread) in daemon threads
    on the fixed PORT, and returns the dashboard's URL.

    Safe to call more than once -- only starts things the first time.
    """
    if state.get("_dashboard_url"):
        return state["_dashboard_url"]

    app = create_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    git_thread = threading.Thread(target=_git_polling_loop, args=(state,), daemon=True)
    git_thread.start()

    for _ in range(50):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)

    url = f"http://127.0.0.1:{PORT}"
    state["_dashboard_url"] = url
    return url
