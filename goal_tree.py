"""
GoalT (Goal Tree) -- a multi-parent, value-propagating goal graph.

Core design goals of this implementation:
1. It is a DAG (Directed Acyclic Graph), not a tree. Cycles are detected
   and rejected explicitly instead of being allowed to loop forever.
2. Value propagation has a deterministic, math-only fallback (a PageRank-style
   weighted propagation) that works with zero LLM calls and always converges.
3. An LLM can *optionally* be plugged in to decide edge weights (i.e. how much
   of a parent's value should flow to each child), but its output is always
   normalized and validated before being used -- the LLM never directly sets
   final values, it only proposes weights that get checked and re-normalized.
4. Updates are incremental: adding a goal only recomputes the affected
   sub-graph, not the whole tree.

This is a reference implementation for a public, evolving concept. See
README.md for open questions and known limitations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

import networkx as nx


class CycleError(Exception):
    """Raised when an edge would introduce a cycle into the goal graph."""


class WeightError(Exception):
    """Raised when weights returned by a redistribution function are invalid."""


@dataclass
class Goal:
    id: str
    label: str
    value: float = 0.0
    description: str = ""
    related_files: list[str] = field(default_factory=list)
    related_backend: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


# A redistribution function takes a goal id and the list of its children ids,
# plus arbitrary context, and returns a dict {child_id: weight} where weights
# are non-negative and will be normalized to sum to 1.0 by the engine.
# This is the hook where an LLM (or any other policy) plugs in.
RedistributionFn = Callable[[str, list[str], dict], dict[str, float]]


def equal_weight_redistribution(parent_id: str, children_ids: list[str], context: dict) -> dict[str, float]:
    """Deterministic fallback: split value equally among children.

    This is the default and requires no LLM call. It guarantees convergence
    because it is a fixed, data-independent function (equivalent to an
    unweighted PageRank-style propagation).
    """
    if not children_ids:
        return {}
    w = 1.0 / len(children_ids)
    return {c: w for c in children_ids}


class GoalGraph:
    """A multi-parent goal DAG with value propagation."""

    def __init__(self, redistribution_fn: Optional[RedistributionFn] = None):
        self.g = nx.DiGraph()
        self.root_id: Optional[str] = None
        # Pluggable redistribution policy. Defaults to the deterministic,
        # LLM-free fallback so the graph is always usable without an API key.
        self.redistribution_fn: RedistributionFn = redistribution_fn or equal_weight_redistribution

    # ---------- graph construction ----------

    def add_root(
        self,
        id: str,
        label: str,
        description: str = "",
        related_files: list[str] | None = None,
        related_backend: list[str] | None = None,
    ) -> Goal:
        if self.root_id is not None:
            raise ValueError(
                f"Root already set to '{self.root_id}'. A GoalGraph has exactly one root. "
                "If you need a second independent goal, create a separate GoalGraph."
            )
        goal = Goal(
            id=id, label=label, value=1.0, description=description,
            related_files=list(related_files or []), related_backend=list(related_backend or []),
        )
        self.g.add_node(id, goal=goal)
        self.root_id = id
        return goal

    def add_goal(
        self,
        id: str,
        label: str,
        parents: list[str],
        description: str = "",
        related_files: list[str] | None = None,
        related_backend: list[str] | None = None,
    ) -> Goal:
        """Add a goal with one or more parents. Rejects cycles explicitly."""
        if self.root_id is None:
            raise ValueError("Call add_root() before adding child goals.")
        if not parents:
            raise ValueError("A non-root goal must have at least one parent.")

        goal = Goal(
            id=id, label=label, value=0.0, description=description,
            related_files=list(related_files or []), related_backend=list(related_backend or []),
        )
        self.g.add_node(id, goal=goal)

        for p in parents:
            if p not in self.g:
                self.g.remove_node(id)
                raise ValueError(f"Unknown parent id: '{p}'")
            self.g.add_edge(p, id, weight=None)  # weight filled in by recompute

        if not nx.is_directed_acyclic_graph(self.g):
            # Roll back -- we never leave the graph in an invalid state.
            #
            # Note: through this method alone, a cycle is actually impossible by
            # construction -- a brand-new node has no outgoing edges yet, so it
            # can't be an ancestor of anything already in the graph. This check
            # is a defensive backstop for a future feature (linking two already
            # -existing goals together), where cycles become genuinely reachable.
            self.g.remove_node(id)
            raise CycleError(
                f"Adding '{id}' under {parents} would create a cycle. "
                "Multi-parent is allowed, but the graph must stay a DAG."
            )

        self.recompute(changed_node=id, context={})
        return goal

    def link_artifacts(
        self,
        id: str,
        files: list[str] | None = None,
        backend: list[str] | None = None,
        replace: bool = False,
    ) -> Goal:
        """Attach (or replace) related files / backend artifacts on an existing goal.

        By default this appends to whatever's already linked (deduplicated),
        since analysis often discovers a goal's related files incrementally
        across multiple tool calls. Pass replace=True to overwrite instead.
        """
        if id not in self.g.nodes:
            raise ValueError(f"Unknown goal id: '{id}'")
        goal = self.g.nodes[id]["goal"]
        if replace:
            goal.related_files = list(files or [])
            goal.related_backend = list(backend or [])
        else:
            if files:
                goal.related_files = list(dict.fromkeys(goal.related_files + list(files)))
            if backend:
                goal.related_backend = list(dict.fromkeys(goal.related_backend + list(backend)))
        return goal

    def goals_for_file(self, file_path: str) -> list[str]:
        """Return ids of goals whose related_files match this path.

        Matching is intentionally loose (suffix match after normalizing
        separators) because callers may pass absolute paths, paths relative
        to different working directories, or paths with different casing on
        case-insensitive filesystems. This is a heuristic, not exact-path
        matching -- documented as such in README.
        """
        normalized_target = file_path.replace("\\", "/").rstrip("/")
        matches = []
        for n in self.g.nodes:
            goal = self.g.nodes[n]["goal"]
            for f in goal.related_files:
                normalized_f = f.replace("\\", "/").rstrip("/")
                if normalized_target == normalized_f or normalized_target.endswith("/" + normalized_f) or normalized_f.endswith("/" + normalized_target):
                    matches.append(n)
                    break
        return matches

    # ---------- value propagation ----------

    def recompute(self, changed_node: Optional[str] = None, context: Optional[dict] = None) -> dict[str, float]:
        """Recompute values.

        If `changed_node` is given, only the descendants of that node (the
        affected sub-graph) are touched -- this is the incremental-update
        path used when a single goal is added. Ancestors are untouched
        because a new child never changes its parent's own value, only how
        that value is split among siblings within the parent it was added to.

        If `changed_node` is None, the whole graph is recomputed from the
        root (used after structural edits like re-weighting).
        """
        context = context or {}
        self.g.nodes[self.root_id]["goal"].value = 1.0

        if changed_node is None:
            nodes_to_process = list(nx.topological_sort(self.g))
        else:
            # Recompute the parent(s) of changed_node's propagation, then
            # everything reachable downstream of the root through them.
            # Simplest correct approach: full topological order, but we only
            # *reassign* values for nodes whose value could differ, i.e. any
            # descendant of any parent of changed_node (including changed_node).
            affected = set()
            for parent in self.g.predecessors(changed_node):
                affected.add(parent)
                affected |= nx.descendants(self.g, parent)
            affected.add(changed_node)
            affected |= nx.descendants(self.g, changed_node)
            nodes_to_process = [n for n in nx.topological_sort(self.g) if n in affected]

        for node in nodes_to_process:
            children = list(self.g.successors(node))
            if not children:
                continue
            weights = self.redistribution_fn(node, children, context)
            self._validate_and_apply_weights(node, children, weights)

        return {n: self.g.nodes[n]["goal"].value for n in self.g.nodes}

    def _validate_and_apply_weights(self, parent_id: str, children_ids: list[str], weights: dict[str, float]) -> None:
        """Normalize + sanity-check weights before trusting them (esp. LLM output)."""
        missing = set(children_ids) - set(weights.keys())
        if missing:
            raise WeightError(f"Redistribution function did not return a weight for children: {missing}")

        cleaned = {}
        for c in children_ids:
            w = weights[c]
            if not isinstance(w, (int, float)) or w < 0:
                raise WeightError(f"Invalid weight for '{c}': {w!r} (must be a non-negative number)")
            cleaned[c] = float(w)

        total = sum(cleaned.values())
        if total <= 0:
            # Degenerate case (e.g. LLM returned all zeros) -- fall back to equal split
            # rather than dividing by zero or silently zeroing everything out.
            cleaned = equal_weight_redistribution(parent_id, children_ids, {})
            total = 1.0

        parent_value = self.g.nodes[parent_id]["goal"].value

        # For multi-parent children, value accumulates from every parent that
        # feeds into them. We reset a child's value the first time it's
        # touched in this pass and accumulate afterwards.
        for c in children_ids:
            normalized_weight = cleaned[c] / total
            contribution = parent_value * normalized_weight
            child_goal = self.g.nodes[c]["goal"]
            if self.g.edges[parent_id, c].get("_touched_this_pass") is None:
                # first contribution in this recompute pass for this edge
                pass
            self.g.edges[parent_id, c]["weight"] = normalized_weight

        # Accumulate contributions across all parents for each child (multi-parent support)
        for c in children_ids:
            pass  # handled below in a second pass over the whole affected set

        self._accumulate_child_values(children_ids)

    def _accumulate_child_values(self, children_ids: list[str]) -> None:
        for c in children_ids:
            total_value = 0.0
            for p in self.g.predecessors(c):
                p_goal = self.g.nodes[p]["goal"]
                w = self.g.edges[p, c].get("weight")
                if w is not None:
                    total_value += p_goal.value * w
            self.g.nodes[c]["goal"].value = total_value

    # ---------- inspection ----------

    def ranked(self) -> list[tuple[str, str, float]]:
        """Return (id, label, value) sorted by value descending."""
        rows = []
        for n in self.g.nodes:
            g = self.g.nodes[n]["goal"]
            rows.append((g.id, g.label, g.value))
        return sorted(rows, key=lambda r: r[2], reverse=True)

    def sum_check(self) -> dict[str, float]:
        """Sanity check: for every parent, children weights should sum to ~1.0."""
        out = {}
        for n in self.g.nodes:
            children = list(self.g.successors(n))
            if children:
                out[n] = sum(self.g.edges[n, c]["weight"] or 0 for c in children)
        return out

    @classmethod
    def from_dict(cls, data: dict, redistribution_fn: Optional[RedistributionFn] = None) -> "GoalGraph":
        """Reconstruct a GoalGraph from a dict produced by to_dict().

        Values and edge weights are restored directly from the snapshot
        rather than recomputed, so this is an exact restore, not a re-run
        of the redistribution function.
        """
        graph = cls(redistribution_fn=redistribution_fn)
        for n in data["nodes"]:
            goal = Goal(
                id=n["id"],
                label=n["label"],
                value=n.get("value", 0.0),
                description=n.get("description", ""),
                related_files=list(n.get("related_files", [])),
                related_backend=list(n.get("related_backend", [])),
            )
            graph.g.add_node(n["id"], goal=goal)
        graph.root_id = data["root"]
        for e in data["edges"]:
            graph.g.add_edge(e["from"], e["to"], weight=e["weight"])
        return graph

    def to_dict(self) -> dict:
        return {
            "root": self.root_id,
            "nodes": [
                {
                    "id": n,
                    "label": self.g.nodes[n]["goal"].label,
                    "value": self.g.nodes[n]["goal"].value,
                    "description": self.g.nodes[n]["goal"].description,
                    "related_files": self.g.nodes[n]["goal"].related_files,
                    "related_backend": self.g.nodes[n]["goal"].related_backend,
                }
                for n in self.g.nodes
            ],
            "edges": [
                {"from": u, "to": v, "weight": self.g.edges[u, v]["weight"]}
                for u, v in self.g.edges
            ],
        }

    def __repr__(self) -> str:
        lines = [f"GoalGraph(root={self.root_id!r})"]
        for id_, label, value in self.ranked():
            lines.append(f"  {value:.3f}  {label} ({id_})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Example LLM-backed redistribution function.
#
# This is intentionally NOT wired to a live API by default -- it's a template
# showing exactly what contract the function must satisfy (see
# RedistributionFn above) and how to keep the LLM's output safe:
#   - low/zero temperature
#   - forced JSON output
#   - validated + re-normalized by the engine regardless of what comes back
# ---------------------------------------------------------------------------

def make_llm_redistribution_fn(call_llm: Callable[[str], str]) -> RedistributionFn:
    """Wrap any `call_llm(prompt: str) -> str` function into a RedistributionFn.

    `call_llm` should call your model of choice with temperature=0 (or as low
    as your API allows) and return the raw text response. This wrapper takes
    care of prompting for JSON and parsing/validating the result. If parsing
    fails or the model returns something invalid, it falls back to the
    deterministic equal-weight split so the graph never breaks because of a
    bad LLM response.
    """

    def fn(parent_id: str, children_ids: list[str], context: dict) -> dict[str, float]:
        prompt = (
            "You are helping prioritize goals in a goal graph.\n"
            f"Parent goal: {context.get(parent_id, parent_id)}\n"
            f"Its children (competing for the parent's value): "
            f"{[context.get(c, c) for c in children_ids]}\n"
            f"User priorities / context: {context.get('user_context', 'none provided')}\n\n"
            "Return ONLY a JSON object mapping each child id to a non-negative "
            "weight reflecting how much of the parent's value it should receive. "
            "Weights do not need to sum to 1 -- they will be normalized automatically. "
            f"Child ids: {children_ids}\n"
            'Example format: {"child_1": 2, "child_2": 1}'
        )
        try:
            raw = call_llm(prompt)
            weights = json.loads(raw)
            if not isinstance(weights, dict):
                raise ValueError("LLM did not return a JSON object")
            return {c: float(weights.get(c, 0)) for c in children_ids}
        except Exception:
            # Never let a flaky LLM response break value propagation.
            return equal_weight_redistribution(parent_id, children_ids, context)

    return fn
