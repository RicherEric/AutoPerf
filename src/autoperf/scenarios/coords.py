from __future__ import annotations

Screen = tuple[int, int]


def rel_point(screen: Screen, fx: float, fy: float) -> tuple[int, int]:
    """Converts fractional (0..1) screen coordinates to absolute device pixels."""
    width, height = screen
    return round(width * fx), round(height * fy)


def rel_tap(screen: Screen, fx: float, fy: float) -> dict:
    x, y = rel_point(screen, fx, fy)
    return {"x": x, "y": y}


def rel_swipe(screen: Screen, fx1: float, fy1: float, fx2: float, fy2: float, duration_ms: int = 300) -> dict:
    x1, y1 = rel_point(screen, fx1, fy1)
    x2, y2 = rel_point(screen, fx2, fy2)
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms}


def long_press(screen: Screen, fx: float, fy: float, duration_ms: int = 800) -> dict:
    """A same-point swipe -- the standard way `adb shell input swipe` simulates a long-press."""
    return rel_swipe(screen, fx, fy, fx, fy, duration_ms)
