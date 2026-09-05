"""Bounded subset-sum search for sweep credits.

The problem
-----------
A bank sometimes collapses several settlements into a single credit. The
statement shows one line for, say, INR 4,18,203.55 and nothing indicates which
settlements it covers. Recovering the composition means finding a subset of
unmatched settlement nets that sums to the credit amount.

Subset-sum is NP-complete in general, so an unbounded search is not an option in
a pipeline that has to finish. Three bounds make it tractable and, more
importantly, make its failure mode honest:

1. **Candidate window.** Only settlements whose settled date falls inside the
   credit's plausible lag window are considered, which cuts the input from every
   settlement to a handful.
2. **Cardinality cap.** Real sweeps collapse two to four cycles, not twenty.
3. **Node budget.** The search counts expansions and gives up when it exceeds the
   budget, reporting that it gave up rather than silently returning "no match".

Why ambiguity is a finding, not a tiebreak
------------------------------------------
The search looks for *up to two* solutions and stops. If it finds exactly one,
that is a match. If it finds two or more, the credit is genuinely explainable in
more than one way and the correct output is an exception for a human, not the
first subset the search happened to reach. Picking one would produce a match that
is both plausible and wrong, and a wrong match in reconciliation is worse than no
match: it closes a break that would otherwise have been investigated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class SubsetSumResult:
    """Outcome of one bounded search.

    ``solutions`` holds indices into the input sequence. It is capped at
    ``max_solutions`` because the caller only ever needs to distinguish none,
    exactly one, and more than one.
    """

    solutions: list[tuple[int, ...]] = field(default_factory=list)
    nodes_expanded: int = 0
    budget_exceeded: bool = False

    @property
    def is_unique(self) -> bool:
        return len(self.solutions) == 1 and not self.budget_exceeded

    @property
    def is_ambiguous(self) -> bool:
        return len(self.solutions) > 1

    @property
    def found_nothing(self) -> bool:
        return not self.solutions and not self.budget_exceeded


def find_subsets(
    values: Sequence[int],
    target: int,
    *,
    tolerance: int = 0,
    min_size: int = 1,
    max_size: int = 4,
    node_budget: int = 20_000,
    max_solutions: int = 2,
) -> SubsetSumResult:
    """Find subsets of ``values`` summing to ``target`` within ``tolerance``.

    All values must be strictly positive. That precondition is what licenses the
    two strongest prunes and the early return on a hit, so it is asserted rather
    than assumed: a zero or negative net reaching this function would silently
    make the search incomplete.
    """
    result = SubsetSumResult()
    count = len(values)
    if count == 0 or max_size <= 0:
        return result
    if any(value <= 0 for value in values):
        raise ValueError("subset-sum requires strictly positive values")

    # Descending order makes the "cannot reach target" prune bite early, because
    # the largest remaining values are considered first.
    order = sorted(range(count), key=lambda i: -values[i])
    ordered_values = [values[i] for i in order]

    # suffix[i] is the sum of ordered_values[i:], used to abandon a branch that
    # cannot reach the target even by taking everything that remains.
    suffix = [0] * (count + 1)
    for i in range(count - 1, -1, -1):
        suffix[i] = suffix[i + 1] + ordered_values[i]

    chosen: list[int] = []

    def dfs(start: int, total: int) -> None:
        if result.budget_exceeded or len(result.solutions) >= max_solutions:
            return

        if len(chosen) >= min_size and abs(total - target) <= tolerance:
            result.solutions.append(tuple(sorted(order[i] for i in chosen)))
            # Every value is positive, so extending this subset can only overshoot.
            # No superset of a solution is a solution, and stopping here is what
            # keeps the solution count meaningful.
            return

        if len(chosen) >= max_size:
            return
        if total > target + tolerance:
            return
        if total + suffix[start] < target - tolerance:
            return

        for i in range(start, count):
            result.nodes_expanded += 1
            if result.nodes_expanded > node_budget:
                result.budget_exceeded = True
                return
            # Taking the largest remaining first; if even that cannot close the
            # gap together with the rest of the suffix, nothing later will.
            if total + suffix[i] < target - tolerance:
                return
            chosen.append(i)
            dfs(i + 1, total + ordered_values[i])
            chosen.pop()
            if result.budget_exceeded or len(result.solutions) >= max_solutions:
                return

    dfs(0, 0)
    return result
