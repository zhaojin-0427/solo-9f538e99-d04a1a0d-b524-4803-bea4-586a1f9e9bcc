from __future__ import annotations

import random

from app.solver import Edge, solve


def _connected(edges, a, b):
    adj = {}
    for e in edges:
        adj.setdefault(e.catalog_a, []).append(e.catalog_b)
        adj.setdefault(e.catalog_b, []).append(e.catalog_a)
    seen = {a}
    stack = [a]
    while stack:
        v = stack.pop()
        for u in adj.get(v, []):
            if u not in seen:
                seen.add(u)
                stack.append(u)
    return b in seen


def _brute(edges, pair1, pair2=None):
    n = len(edges)
    best = None
    for mask in range(1 << n):
        kept = []
        cost = 0
        ok = True
        for i, edge in enumerate(edges):
            if mask >> i & 1:
                if edge.locked:
                    ok = False
                    break
                cost += edge.minutes
            else:
                kept.append(edge)
        if not ok:
            continue
        if _connected(kept, *pair1):
            continue
        if pair2 is not None and _connected(kept, *pair2):
            continue
        if best is None or cost < best:
            best = cost
    return best


def test_single_pair_simple_path():
    edges = [
        Edge(1, "V1", "A", "B", 5),
        Edge(2, "V2", "A", "B", 3),
        Edge(3, "V3", "B", "C", 7),
    ]
    result = solve(edges, ("A", "C"))
    assert result is not None
    # Both A-B credentials together cost 8; the single B-C credential costs 7.
    assert result.total == 7
    assert {e.voucher_no for e in result.items} == {"V3"}


def test_parallel_credentials_all_charged():
    edges = [
        Edge(1, "V1", "A", "B", 4),
        Edge(2, "V2", "A", "B", 6),
    ]
    result = solve(edges, ("A", "B"))
    assert result is not None
    assert result.total == 10


def test_locked_credential_forces_alternative():
    edges = [
        Edge(1, "V1", "A", "B", 1, locked=True),
        Edge(2, "V2", "B", "C", 8),
    ]
    result = solve(edges, ("A", "C"))
    assert result is not None
    assert result.total == 8


def test_unsolvable_when_locked_bridge():
    edges = [
        Edge(1, "V1", "A", "B", 5, locked=True),
    ]
    assert solve(edges, ("A", "B")) is None


def test_two_pairs_share_credential():
    # X-Y is the only bridge between the two clusters, so one credential
    # separates both pairs.
    edges = [
        Edge(1, "V1", "A", "X", 5),
        Edge(2, "V2", "B", "Y", 5),
        Edge(3, "V3", "X", "Y", 4),
        Edge(4, "V4", "C", "X", 5),
        Edge(5, "V5", "D", "Y", 5),
    ]
    result = solve(edges, ("A", "B"), ("C", "D"))
    assert result is not None
    assert result.total == 4
    assert [e.voucher_no for e in result.items] == ["V3"]


def test_two_pairs_cannot_share_terminal_conflict():
    # Pairs with a common number: only patterns that keep the shared number
    # on one side are considered.
    edges = [
        Edge(1, "V1", "A", "B", 2),
        Edge(2, "V2", "A", "C", 3),
        Edge(3, "V3", "B", "C", 5),
    ]
    result = solve(edges, ("A", "B"), ("A", "C"))
    assert result is not None
    kept = [e for e in edges if e.credential_id not in {x.credential_id for x in result.items}]
    assert not _connected(kept, "A", "B")
    assert not _connected(kept, "A", "C")
    assert result.total == _brute(edges, ("A", "B"), ("A", "C"))


def test_random_against_brute_force():
    rng = random.Random(2026)
    names = "ABCDE"
    checked = 0
    for _ in range(1500):
        count = rng.randint(1, 8)
        edges = []
        for i in range(count):
            a, b = rng.sample(names, 2)
            edges.append(
                Edge(i, f"V{i}", a, b, rng.randint(1, 9), locked=rng.random() < 0.15)
            )
        pair1 = tuple(rng.sample(names, 2))
        pair2 = None
        if rng.random() < 0.6:
            pair2 = tuple(rng.sample(names, 2))
            if pair2 == pair1 or pair2 == pair1[::-1]:
                continue
        expected = _brute(edges, pair1, pair2)
        result = solve(edges, pair1, pair2)
        actual = None if result is None else result.total
        assert actual == expected, (edges, pair1, pair2, expected, actual)
        if result is not None:
            chosen = {e.credential_id for e in result.items}
            kept = [e for e in edges if e.credential_id not in chosen]
            assert not _connected(kept, *pair1)
            if pair2 is not None:
                assert not _connected(kept, *pair2)
            assert result.total == sum(e.minutes for e in result.items)
        checked += 1
    assert checked > 1000


def test_scales_to_2000_credentials_quickly():
    rng = random.Random(9)
    edges = []
    for i in range(2000):
        a = f"C{rng.randrange(400):03d}"
        b = f"C{rng.randrange(400):03d}"
        if a == b:
            b = f"C{(int(b[1:]) + 1) % 400:03d}"
        edges.append(Edge(i, f"V{i:04d}", a, b, rng.randint(1, 10000)))
    result = solve(edges, ("C000", "C001"), ("C002", "C003"))
    assert result is not None
    kept = [e for e in edges if e.credential_id not in {x.credential_id for x in result.items}]
    assert not _connected(kept, "C000", "C001")
    assert not _connected(kept, "C002", "C003")
