"""
Local, interactive dashboard for GoalT. Serves a single HTML page with a
draggable/zoomable graph (vis-network, loaded from a CDN in the browser)
that polls the running MCP server's in-memory graph and re-renders on
change -- without discarding node positions you've dragged.

Two ways "activity" reaches the browser, and they're deliberately different
guarantees:

1. Generic activity pulse (guaranteed): Claude Code hooks (PreToolUse) POST
   to /hooks/pre-tool-use on every tool call, regardless of what it's doing.
   This always fires -- it's wired into Claude Code's lifecycle, not
   dependent on the model choosing to do anything extra.

2. Per-goal highlighting (best-effort): the MCP server's set_active_goal
   tool marks specific goal ids as "being worked on right now". This only
   happens if Claude chooses to call that tool as part of its plan -- the
   server instructions ask it to, but nothing enforces it. Don't oversell
   this as guaranteed; it's a convention, not a hard guarantee.

The dashboard runs on a fixed port (see PORT below) rather than scanning for
a free one, because the hook URL Claude Code is configured to call is a
static string in plugin.json -- it has to know where to find us in advance.
"""

from __future__ import annotations

import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from goal_tree import GoalGraph

PORT = 8765


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
    #panel { width: 300px; border-left: 1px solid #2a2d35; padding: 16px; overflow-y: auto; display: none; }
    #panel.open { display: block; }
    #panel h2 { font-size: 15px; margin: 0 0 4px; }
    #panel .value { font-size: 12px; color: #9aa0a8; margin-bottom: 12px; }
    #panel .desc { font-size: 13px; line-height: 1.5; color: #cfd3da; white-space: pre-wrap; }
    #panel .empty { font-size: 13px; color: #6b7078; font-style: italic; }
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
      <h2 id="panelTitle"></h2>
      <div class="value" id="panelValue"></div>
      <div class="desc" id="panelDesc"></div>
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

    // Once a person drags a node, freeze its position so the next poll()
    // doesn't yank it back -- that's the "stop the graph from constantly
    // rearranging itself" fix.
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
    network.on('click', (params) => {
      if (!params.nodes.length) { panel.classList.remove('open'); return; }
      const node = nodesDS.get(params.nodes[0]);
      document.getElementById('panelTitle').textContent = node.rawLabel;
      document.getElementById('panelValue').textContent = 'value: ' + node.value.toFixed(3);
      const descEl = document.getElementById('panelDesc');
      if (node.description) {
        descEl.textContent = node.description;
        descEl.classList.remove('empty');
      } else {
        descEl.textContent = 'No description set for this goal.';
        descEl.classList.add('empty');
      }
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
        const activeIds = new Set(Object.keys(data.active_goals || {}));

        const existingIds = new Set(nodesDS.getIds());
        const incomingIds = new Set(data.nodes.map(n => n.id));

        const visNodes = data.nodes.map(n => {
          const isActive = activeIds.has(n.id);
          const base = {
            id: n.id,
            rawLabel: n.label,
            description: n.description || '',
            value: n.value,
            label: `${n.label}\\n${n.value.toFixed(2)}`,
            size: 14 + 26 * (n.value / maxVal),
            color: { background: shade(n.value / maxVal), border: isActive ? '#4ade80' : '#e8e8e8' },
            borderWidth: isActive ? 4 : 2,
          };
          if (isActive) {
            base.shadow = { enabled: true, color: 'rgba(74, 222, 128, 0.6)', size: 20, x: 0, y: 0 };
          } else {
            base.shadow = { enabled: false };
          }
          return base;
        });

        // Update in place (not setData) so dragged/fixed positions survive.
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
            return JSONResponse({"nodes": [], "edges": [], "active_goals": {}})
        payload = graph.to_dict()
        payload["active_goals"] = state.get("active_goals", {})
        return JSONResponse(payload)

    @app.get("/api/activity")
    def api_activity():
        return JSONResponse({"last_activity": state.get("last_activity")})

    # --- Claude Code hook endpoints -------------------------------------
    # These are hit by Claude Code itself (via the "http" hook type in
    # plugin.json), not by the browser. They only ever observe and record
    # -- they always allow the tool call, never block it.

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
        # Explicit allow -- this hook only observes, it must never gate.
        return JSONResponse({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        })

    return app


def start_dashboard_in_background(state: dict) -> str:
    """Starts the dashboard in a daemon thread on the fixed PORT and returns its URL.

    Safe to call more than once -- only starts the server the first time.
    Raises if the port is already taken by something else, since the hook
    URL Claude Code is configured with points at this exact port.
    """
    if state.get("_dashboard_url"):
        return state["_dashboard_url"]

    app = create_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Give uvicorn a moment to bind so a caller can immediately curl the URL.
    for _ in range(50):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)

    url = f"http://127.0.0.1:{PORT}"
    state["_dashboard_url"] = url
    return url
