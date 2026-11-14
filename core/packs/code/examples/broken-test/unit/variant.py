"""Clamp a number into a closed interval."""


def clamp(value: int, low: int, high: int) -> int:
    """Return value limited to the closed interval [low, high]."""
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, value)
