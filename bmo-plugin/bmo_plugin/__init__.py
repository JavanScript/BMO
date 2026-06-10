try:
    from .plugin import BMO
except ImportError as exc:
    if exc.name != "maubot":
        raise
    BMO = None  # type: ignore[assignment]

__all__ = ["BMO"]

