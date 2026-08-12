"""Reproducible quality/runtime benchmark for the seating optimizer.

Run with ``.venv/bin/python tools/benchmark_seating_optimizer.py``.  The small
random rooms compare the bounded structured search to the exhaustive oracle;
the large open-room cases measure the production solver end to end.
"""

from __future__ import annotations

import random
import time

from screenwatch.seating.groups import (
    SeatRequest,
    _exact_groups,
    _rank_groups,
    _search_groups,
    clear_seating_cache,
    find_groups,
    seating_cache_info,
)
from screenwatch.seating.quality import QualityModel
from screenwatch.seating.render import build_auditorium


def oracle_trials(seed: int = 20260811, trials: int = 40) -> None:
    rng = random.Random(seed)
    regrets: list[float] = []
    misses = 0
    for _ in range(trials):
        layout = [
            "".join("." if rng.random() < 0.68 else "×" for _ in range(8))
            for _ in range(4)
        ]
        room = build_auditorium("benchmark", "oracle", layout)
        party_size = rng.randint(2, 4)
        if room.available < party_size:
            continue
        request = SeatRequest(party_size)
        model = QualityModel().for_auditorium(room)
        exact_result = _exact_groups(room, request, model)
        if exact_result is None:
            continue
        exact, _ = exact_result
        structured = _search_groups(room, request, model, limit=1)
        exact_best = _rank_groups(exact, 1)
        structured_best = _rank_groups(structured, 1)
        if not exact_best or not structured_best:
            continue
        regret = max(
            0.0,
            exact_best[0].robust_preference_score
            - structured_best[0].robust_preference_score,
        )
        regrets.append(regret)
        misses += regret > 1e-6

    print(
        "oracle",
        f"trials={len(regrets)}",
        f"exact_score_misses={misses}",
        f"mean_regret={sum(regrets) / max(len(regrets), 1):.6f}",
        f"max_regret={max(regrets, default=0.0):.6f}",
    )


def runtime_trials() -> None:
    room = build_auditorium("benchmark", "large", ["." * 24] * 10)
    clear_seating_cache()
    for party_size in (1, 2, 3, 5, 10, 15, 20):
        started = time.perf_counter()
        best = find_groups(room, party_size, limit=1)[0]
        elapsed = time.perf_counter() - started
        print(
            "runtime",
            f"party={party_size}",
            f"seconds={elapsed:.4f}",
            f"parts={[len(part) for part in best.parts]}",
            f"robust={best.robust_preference_score:.4f}",
            f"method={best.certificate.method if best.certificate else 'none'}",
        )
    started = time.perf_counter()
    find_groups(room, 20, limit=1)
    print(
        "cache",
        f"warm_party_20_seconds={time.perf_counter() - started:.6f}",
        f"info={seating_cache_info()}",
    )


if __name__ == "__main__":
    oracle_trials()
    runtime_trials()
