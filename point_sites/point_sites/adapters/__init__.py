"""Adapter registry — maps ``--site <name>`` to the corresponding ``Adapter``.

Adding a new site is a two-line change here plus the adapter module
itself (``adapters/<name>/__init__.py`` exporting ``ADAPTER``). The CLI
argument ``--site`` and the GitHub Actions workflow file naming both
follow the keys of ``REGISTRY``.
"""

from ..common.adapter import Adapter
from .amefuri import ADAPTER as AMEFURI
from .chanceit import ADAPTER as CHANCEIT
from .fruitmail import ADAPTER as FRUITMAIL
from .fruitmail_lottery import ADAPTER as FRUITMAIL_LOTTERY
from .gendama import ADAPTER as GENDAMA
from .getmoney import ADAPTER as GETMONEY
from .hapitas import ADAPTER as HAPITAS
from .moppy import ADAPTER as MOPPY
from .pointincome import ADAPTER as POINTINCOME
from .pointtown import ADAPTER as POINTTOWN
from .sugutama import ADAPTER as SUGUTAMA
from .warau import ADAPTER as WARAU

REGISTRY: dict[str, Adapter] = {
    MOPPY.name: MOPPY,
    POINTINCOME.name: POINTINCOME,
    HAPITAS.name: HAPITAS,
    GENDAMA.name: GENDAMA,
    AMEFURI.name: AMEFURI,
    POINTTOWN.name: POINTTOWN,
    GETMONEY.name: GETMONEY,
    FRUITMAIL.name: FRUITMAIL,
    FRUITMAIL_LOTTERY.name: FRUITMAIL_LOTTERY,
    WARAU.name: WARAU,
    SUGUTAMA.name: SUGUTAMA,
    CHANCEIT.name: CHANCEIT,
}


def get_adapter(name: str) -> Adapter:
    if name not in REGISTRY:
        available = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown site {name!r}. Available: {available}")
    return REGISTRY[name]
