"""Base classes shared by every blockchain provider."""

from __future__ import annotations

__version__ = "0.1.25"






from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class TokenBalance:
    symbol: str
    name: str
    amount: float
    raw_amount: str | None = None  # original on-chain string value, if useful
    contract: str | None = None
    usd_value: float | None = None
    eur_value: float | None = None
    #: kind of asset, e.g. "token" (fungible, default), "nft", "sft",
    #: "meta-esdt". Chains without that distinction just leave it as "token".
    asset_type: str = "token"


@dataclass
class WalletBalance:
    chain: str
    address: str
    native_symbol: str
    native_amount: float | None = None
    native_usd_value: float | None = None
    native_eur_value: float | None = None
    tokens: list[TokenBalance] = field(default_factory=list)
    error: str | None = None
    warning: str | None = None
    #: sum of native_usd_value + every priced token/staking usd_value
    total_usd: float | None = None
    total_eur: float | None = None
    #: optional group name from the input file (e.g. "[Perso]"), so several
    #: addresses/wallets can be rolled up into one combined total. None for
    #: an address listed outside any [Label] section.
    label: str | None = None
    #: number of tokens/positions removed from `tokens` because they had no
    #: usd_value/eur_value (dust, illiquid ESDT, NFTs/SFTs...). Only set when
    #: the default "priced only" filtering ran; 0 otherwise (including when
    #: --show-unpriced or --no-price was used, since nothing gets filtered).
    hidden_unpriced_count: int = 0
    #: number of tokens/positions (and the native coin line, if applicable)
    #: hidden because their value is below the dust threshold (1 cent USD by
    #: default). Only set when the default filtering ran; 0 with --show-dust,
    #: --show-unpriced or --no-price.
    hidden_dust_count: int = 0
    #: True when the native coin balance itself was hidden for being below the
    #: dust threshold (the native_* fields keep their values, so totals and
    #: JSON data stay intact; only the on-screen/CSV native line is skipped).
    native_hidden: bool = False


class BaseProvider(ABC):
    """Interface every blockchain provider must implement."""

    #: short machine-readable id, e.g. "bitcoin", "ethereum"
    chain_id: str = "unknown"
    #: human readable name, e.g. "Bitcoin"
    display_name: str = "Unknown"
    #: symbol of the native coin, e.g. "BTC"
    native_symbol: str = "?"

    @classmethod
    @abstractmethod
    def matches(cls, address: str) -> bool:
        """Return True if `address` looks like an address for this chain."""
        raise NotImplementedError

    @abstractmethod
    def get_balance(self, address: str) -> WalletBalance:
        """Fetch native coin balance and token balances for `address`."""
        raise NotImplementedError
