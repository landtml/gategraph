"""Clamp a number into a closed interval."""


def clamp(value: int, low: int, high: int) -> int
    """Return value limited to the closed interval [low, high]."""
    return max(low, min(value, high))
