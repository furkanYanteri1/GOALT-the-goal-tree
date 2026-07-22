"""
Local, interactive dashboard for GoalT. Serves a single HTML page with a
draggable/zoomable graph (vis-network, loaded from a CDN in the browser)
that polls the running MCP server's in-memory graph and re-renders on
change. No build step, no separate frontend project -- one file.

This is intentionally simple: polling instead of websockets, a single
inline HTML template instead of a frontend framework. If this needs to
get fancier later (websocket push, node editing from the browser), that's
a natural next step -- not needed for a concept-stage tool.
"""

from __future__ import annotations

import socket
import threading

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from goal_tree import GoalGraph


def find_free_port(start: int = 8765, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No free port found in range {start}-{start + tries}")


PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>GoalT</title>
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <style>
    body { margin: 0; font-family: -apple-system, Segoe UI, sans-serif; background: #111318; color: #e8e8e8; }
    #header { padding: 12px 20px; border-bottom: 1px solid #2a2d35; display: flex; align-items: center; justify-content: space-between; }
    #header h1 { font-size: 16px; margin: 0; font-weight: 600; }
    #status { font-size: 12px; color: #7d8590; }
    #network { width: 100vw; height: calc(100vh - 46px); }
    #empty { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: #7d8590; text-align: center; }
  </style>
</head>
<body>
  <div id="header">
    <h1>GoalT</h1>
    <span id="status">connecting...</span>
  </div>
  <div id="network"></div>
  <div id="empty" style="display:none">No tree yet. Ask Claude to create one.</div>

  <script>
    const container = document.getElementById('network');
    const statusEl = document.getElementById('status');
    const emptyEl = document.getElementById('empty');
    const network = new vis.Network(container, { nodes: new vis.DataSet([]), edges: new vis.DataSet([]) }, {
      nodes: {
        shape: 'dot',
        font: { color: '#e8e8e8', size: 13 },
        borderWidth: 2,
      },
      edges: {
        arrows: 'to',
        color: { color: '#4a4d55', highlight: '#8b8f99' },
        font: { color: '#7d8590', size: 10, strokeWidth: 0 },
        smooth: { type: 'cubicBezier', roundness: 0.4 },
      },
      physics: { stabilization: true, barnesHut: { gravitationalConstant: -4000, springLength: 140 } },
      interaction: { hover: true, zoomView: true, dragView: true },
    });

    let lastSignature = null;

    async function poll() {
      try {
        const res = await fetch('/api/graph');
        const data = await res.json();
        statusEl.textContent = 'live · updates automatically';

        if (!data.nodes.length) {
          emptyEl.style.display = 'block';
          network.setData({ nodes: new vis.DataSet([]), edges: new vis.DataSet([]) });
          lastSignature = null;
          return;
        }
        emptyEl.style.display = 'none';

        const signature = JSON.stringify(data);
        if (signature === lastSignature) return;
        lastSignature = signature;

        const maxVal = Math.max(...data.nodes.map(n => n.value), 0.001);
        const visNodes = data.nodes.map(n => ({
          id: n.id,
          label: `${n.label}\\n${n.value.toFixed(2)}`,
          value: n.value,
          size: 14 + 26 * (n.value / maxVal),
          color: { background: shade(n.value / maxVal), border: '#e8e8e8' },
        }));
        const visEdges = data.edges.map(e => ({
          from: e.from, to: e.to,
          label: e.weight !== null ? e.weight.toFixed(2) : '',
        }));

        network.setData({ nodes: new vis.DataSet(visNodes), edges: new vis.DataSet(visEdges) });
      } catch (err) {
        statusEl.textContent = 'disconnected — is the MCP server still running?';
      }
    }

    function shade(t) {
      // low value -> muted blue, high value -> warm amber
      const r = Math.round(60 + t * 195);
      const g = Math.round(90 + t * 100);
      const b = Math.round(160 - t * 100);
      return `rgb(${r}, ${g}, ${b})`;
    }

    poll();
    setInterval(poll, 1500);
  </script>
</body>
</html>
"""


def create_app(state: dict) -> FastAPI:
    """state is the same dict the MCP server holds its GoalGraph in, e.g. {'graph': GoalGraph | None}."""
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/api/graph")
    def api_graph():
        graph: GoalGraph | None = state.get("graph")
        if graph is None:
            return JSONResponse({"nodes": [], "edges": []})
        return JSONResponse(graph.to_dict())

    return app


def start_dashboard_in_background(state: dict, port: int | None = None) -> str:
    """Starts the dashboard in a daemon thread and returns its URL. Safe to call more than once -- only starts the server on the first call."""
    if state.get("_dashboard_url"):
        return state["_dashboard_url"]

    chosen_port = port or find_free_port()
    app = create_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=chosen_port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{chosen_port}"
    state["_dashboard_url"] = url
    return url
