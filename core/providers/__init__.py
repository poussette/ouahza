"""
Provider registry for blockchain wallet lookups.

Each provider module exposes a class implementing BaseProvider (see base.py)
and registers itself in PROVIDERS below. To add a new blockchain:

  1. Create providers/<chain>.py implementing BaseProvider.
  2. Add a `matches(address)` classmethod for auto-detection.
  3. Import and register it below.

That's the only file that needs touching to plug in a new chain.
"""

__version__ = "0.1.16"






from .base import BaseProvider
from .bitcoin import BitcoinProvider
from .ethereum import EthereumProvider
from .solana import SolanaProvider
from .multiversx import MultiversXProvider

# Order matters for auto-detection: first matching provider wins.
PROVIDERS: list[type[BaseProvider]] = [
    BitcoinProvider,
    EthereumProvider,
    SolanaProvider,
    MultiversXProvider,
]

PROVIDERS_BY_NAME: dict[str, type[BaseProvider]] = {
    p.chain_id: p for p in PROVIDERS
}


def detect_provider(address: str) -> type[BaseProvider] | None:
    """Guess which chain an address belongs to, based on its format."""
    address = address.strip()
    for provider_cls in PROVIDERS:
        try:
            if provider_cls.matches(address):
                return provider_cls
        except Exception:
            continue
    return None


def get_provider_by_name(name: str) -> type[BaseProvider] | None:
    return PROVIDERS_BY_NAME.get(name.strip().lower())
