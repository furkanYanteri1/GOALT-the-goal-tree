"""Visualization helpers for GoalGraph -- kept separate from core logic
so goal_tree.py has no plotting dependency."""

import matplotlib.pyplot as plt
import networkx as nx


def draw(graph, title="Goal Tree", figsize=(9, 6), ax=None):
    """Draw the goal graph, node size/color reflecting current value."""
    g = graph.g
    pos = nx.nx_agraph.graphviz_layout(g, prog="dot") if _has_graphviz() else nx.spring_layout(g, seed=42, k=1.2)

    values = [g.nodes[n]["goal"].value for n in g.nodes]
    labels = {n: f'{g.nodes[n]["goal"].label}\n{g.nodes[n]["goal"].value:.2f}' for n in g.nodes}

    max_v = max(values) if values else 1.0
    sizes = [1200 + 3500 * (v / max_v if max_v > 0 else 0) for v in values]
    colors = values

    created_fig = False
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
        created_fig = True

    nx.draw_networkx_edges(g, pos, ax=ax, arrows=True, arrowsize=15, edge_color="#999999", connectionstyle="arc3,rad=0.05")
    nodes = nx.draw_networkx_nodes(g, pos, ax=ax, node_size=sizes, node_color=colors, cmap="YlOrRd", vmin=0, vmax=max_v)
    nx.draw_networkx_labels(g, pos, labels=labels, ax=ax, font_size=8)

    edge_labels = {(u, v): f'{g.edges[u, v]["weight"]:.2f}' for u, v in g.edges if g.edges[u, v]["weight"] is not None}
    nx.draw_networkx_edge_labels(g, pos, edge_labels=edge_labels, ax=ax, font_size=7, font_color="#555555")

    ax.set_title(title)
    ax.axis("off")
    if created_fig:
        plt.tight_layout()
        plt.show()


def _has_graphviz() -> bool:
    try:
        import pygraphviz  # noqa: F401
        return True
    except ImportError:
        return False
