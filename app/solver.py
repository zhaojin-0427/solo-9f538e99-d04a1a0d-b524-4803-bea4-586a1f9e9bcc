"""Exact minimum deactivation solver.

The catalogue graph has one vertex per catalogue number and one undirected
edge per credential (weight = expected review minutes).  A locked credential
gets infinite weight because it may not be deactivated.

Selecting credentials whose removal disconnects every confirmed-different
pair is the (undirected) minimum multicut problem.  For one or two pairs this
is solvable exactly in polynomial time:

* one pair  -> an ordinary minimum s-t cut;
* two pairs -> an optimum is always a single cut d(S) (take the connected
  components surviving after removing an optimal edge set; a subset of those
  components separates both pairs and its boundary is contained in the
  removed set).  For each of the four ways to put exactly one endpoint of
  each pair into S, compute a minimum cut with those endpoints pinned to the
  source/sink.  The cheapest of the four is the exact two-pair optimum.

No subset of credentials is ever enumerated.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

# Cheap safety net for the recursive blocking-flow DFS.  The graph is at most
# ~4000 vertices, so this is comfortably above the longest possible path.
if sys.getrecursionlimit() < 100_000:
    sys.setrecursionlimit(100_000)


class SolveCancelled(Exception):
    """Raised when a running solve is aborted (e.g. the feed was replaced).

    Cooperative cancellation: the worker passes a callback that returns True
    once the job has been voided, and the solver checks it at every
    coarse-grained loop so it stops promptly without holding any DB lock.
    """


@dataclass(frozen=True)
class Edge:
    credential_id: int
    voucher_no: str
    catalog_a: str
    catalog_b: str
    minutes: int
    locked: bool = False


@dataclass(frozen=True)
class SolveResult:
    total: int
    items: tuple[Edge, ...]
    pattern_index: int


class _MaxFlow:
    """Dinic max flow with explicit residual arcs.

    Undirected edges are represented by a pair of mutually reverse arcs that
    both start with capacity c.  This is the usual undirected-capacity model:
    a cut through the edge pays c once, and flow may run either way.
    """

    __slots__ = ("n", "to", "cap", "adj")

    def __init__(self, n: int) -> None:
        self.n = n
        self.to: list[int] = []
        self.cap: list[int] = []
        self.adj: list[list[int]] = [[] for _ in range(n)]

    def add_undirected(self, u: int, v: int, capacity: int) -> None:
        i = len(self.to)
        self.to.append(v)
        self.cap.append(capacity)
        self.adj[u].append(i)
        self.to.append(u)
        self.cap.append(capacity)
        self.adj[v].append(i + 1)

    def add_directed(self, u: int, v: int, capacity: int) -> None:
        i = len(self.to)
        self.to.append(v)
        self.cap.append(capacity)
        self.adj[u].append(i)
        self.to.append(u)
        self.cap.append(0)
        self.adj[v].append(i + 1)

    def _bfs(
        self, s: int, t: int, cancel_cb: Callable[[], bool] | None
    ) -> list[int]:
        level = [-1] * self.n
        level[s] = 0
        queue = [s]
        head = 0
        checks = 0
        while head < len(queue):
            v = queue[head]
            head += 1
            for eid in self.adj[v]:
                if self.cap[eid] > 0:
                    u = self.to[eid]
                    if level[u] < 0:
                        level[u] = level[v] + 1
                        queue.append(u)
            checks += 1
            if cancel_cb is not None and (checks & 63) == 0 and cancel_cb():
                raise SolveCancelled()
        return level

    def _dfs(self, v: int, t: int, pushed: int, level: list[int], it: list[int]) -> int:
        if v == t:
            return pushed
        adj_v = self.adj[v]
        while it[v] < len(adj_v):
            eid = adj_v[it[v]]
            u = self.to[eid]
            if self.cap[eid] > 0 and level[u] == level[v] + 1:
                got = self._dfs(u, t, min(pushed, self.cap[eid]), level, it)
                if got:
                    self.cap[eid] -= got
                    self.cap[eid ^ 1] += got
                    return got
            it[v] += 1
        return 0

    def max_flow(
        self,
        s: int,
        t: int,
        limit: int | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> int:
        if s == t:
            return 0
        bound = limit if limit is not None else 1 << 120
        flow = 0
        while flow < bound:
            if cancel_cb is not None and cancel_cb():
                raise SolveCancelled()
            level = self._bfs(s, t, cancel_cb)
            if level[t] < 0:
                break
            it = [0] * self.n
            while flow < bound:
                pushed = self._dfs(s, t, bound - flow, level, it)
                if not pushed:
                    break
                flow += pushed
        return flow

    def reachable(self, s: int) -> list[bool]:
        seen = [False] * self.n
        seen[s] = True
        stack = [s]
        while stack:
            v = stack.pop()
            for eid in self.adj[v]:
                if self.cap[eid] > 0:
                    u = self.to[eid]
                    if not seen[u]:
                        seen[u] = True
                        stack.append(u)
        return seen


def _node_index(edges: Sequence[Edge], terminals: Iterable[str]) -> dict[str, int]:
    catalogs = {e.catalog_a for e in edges} | {e.catalog_b for e in edges}
    catalogs.update(terminals)
    return {catalog: i for i, catalog in enumerate(sorted(catalogs))}


def _cut_with_pins(
    edges: Sequence[Edge],
    index: dict[str, int],
    source_terminals: Sequence[str],
    sink_terminals: Sequence[str],
    inf: int,
    cancel_cb: Callable[[], bool] | None = None,
) -> tuple[int, list[Edge]] | None:
    """Minimum cut separating the pinned source terminals from the pinned sink
    terminals.  Returns None when every such cut must deactivate a locked
    credential."""

    base = len(index)
    source, sink = base, base + 1
    network = _MaxFlow(base + 2)
    for edge in edges:
        capacity = inf if edge.locked else edge.minutes
        network.add_undirected(
            index[edge.catalog_a], index[edge.catalog_b], capacity
        )
    for terminal in source_terminals:
        network.add_directed(source, index[terminal], inf)
    for terminal in sink_terminals:
        network.add_directed(index[terminal], sink, inf)

    flow = network.max_flow(source, sink, limit=inf, cancel_cb=cancel_cb)
    if flow >= inf:
        return None

    side = network.reachable(source)
    chosen = [
        edge
        for edge in edges
        if side[index[edge.catalog_a]] != side[index[edge.catalog_b]]
    ]
    weight = sum(edge.minutes for edge in chosen)
    if weight != flow:  # pragma: no cover - internal invariant
        raise RuntimeError(
            f"min-cut extraction mismatch: weight={weight} flow={flow}"
        )
    return weight, chosen


def _pair_patterns(
    pair1: tuple[str, str], pair2: tuple[str, str]
) -> list[list[tuple[str, str]]]:
    """The consistent pinning patterns for two pairs.

    Each pattern is [[source terminals], [sink terminals]].  Patterns where a
    shared catalogue number would have to be on both sides are discarded.
    """
    patterns: list[list[tuple[str, str]]] = []
    for pick1 in pair1:
        for pick2 in pair2:
            source = {pick1, pick2}
            sink = {x for x in pair1 if x != pick1} | {
                x for x in pair2 if x != pick2
            }
            if source & sink:
                continue
            patterns.append([sorted(source), sorted(sink)])
    return patterns


def solve(
    edges: Sequence[Edge],
    pair1: tuple[str, str],
    pair2: tuple[str, str] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> SolveResult | None:
    """Return a minimum-total deactivation list, or None if impossible.

    ``edges`` must contain only currently effective (active) credentials.
    Locked credentials are treated as undeactivatable.  When ``cancel_cb``
    returns True, the in-flight computation raises :class:`SolveCancelled`
    (before any result is returned or written anywhere).
    """

    edges = tuple(edges)
    inf = sum(edge.minutes for edge in edges) + 1

    if pair2 is None:
        index = _node_index(edges, [*pair1])
        found = _cut_with_pins(
            edges, index, [pair1[0]], [pair1[1]], inf, cancel_cb
        )
        if found is None:
            return None
        weight, chosen = found
        return SolveResult(weight, tuple(_ordered(chosen)), 0)

    patterns = _pair_patterns(pair1, pair2)
    index = _node_index(edges, [*pair1, *pair2])
    best: tuple[int, tuple[Edge, ...], int] | None = None
    for pattern_index, (source_terms, sink_terms) in enumerate(patterns):
        if cancel_cb is not None and cancel_cb():
            raise SolveCancelled()
        found = _cut_with_pins(
            edges, index, source_terms, sink_terms, inf, cancel_cb
        )
        if found is None:
            continue
        weight, chosen = found
        ordered = tuple(_ordered(chosen))
        candidate = (weight, ordered, pattern_index)
        if best is None:
            best = candidate
        else:
            # Deterministic tie-break: cheaper first, then the list whose
            # credential ids sort first.
            best_key = (best[0], tuple(e.credential_id for e in best[1]))
            cand_key = (weight, tuple(e.credential_id for e in ordered))
            if cand_key < best_key:
                best = candidate
    if best is None:
        return None
    return SolveResult(best[0], best[1], best[2])


def _ordered(edges: Iterable[Edge]) -> list[Edge]:
    return sorted(edges, key=lambda e: (e.voucher_no, e.credential_id))
