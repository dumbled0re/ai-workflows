"""Adapter registry — maps ``--site <name>`` to the corresponding ``Adapter``.

Adding a new site is a two-line change here plus the adapter module
itself (``adapters/<name>/__init__.py`` exporting ``ADAPTER``). The CLI
argument ``--site`` and the GitHub Actions workflow file naming both
follow the keys of ``REGISTRY``.
"""

from ..common.adapter import Adapter
from .moppy import ADAPTER as MOPPY
from .pointincome import ADAPTER as POINTINCOME

REGISTRY: dict[str, Adapter] = {
    MOPPY.name: MOPPY,
    POINTINCOME.name: POINTINCOME,
}


def get_adapter(name: str) -> Adapter:
    if name not in REGISTRY:
        available = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown site {name!r}. Available: {available}")
    return REGISTRY[name]
