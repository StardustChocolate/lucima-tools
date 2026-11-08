"""Abyss star fallback for a confirmed win with no returned star conditions."""
from __future__ import annotations


def stars_from_counter(counter: int | None, *, won: bool) -> int | None:
    """Use the stage's TurnRoleTurn counter, never individual actor turns."""
    if not won:
        return 0
    if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
        return None
    for maximum, stars in ((10, 4), (12, 3), (15, 2), (20, 1)):
        if counter <= maximum:
            return stars
    return 0


def resolve_stars(stars, counter: int | None, *, won: bool) -> tuple[int | None, str]:
    """Server's four star-condition flags take precedence over the fallback."""
    if isinstance(stars, list) and len(stars) >= 4:
        return sum(bool(flag) for flag in stars[:4]), 'server'
    inferred = stars_from_counter(counter, won=won)
    if inferred is not None:
        return inferred, 'counter'
    if isinstance(stars, list) and stars:
        return sum(bool(flag) for flag in stars), 'server_partial'
    return None, 'unknown'
