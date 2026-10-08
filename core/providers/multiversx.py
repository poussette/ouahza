"""MultiversX (formerly Elrond) provider.

Backed by the free public MultiversX REST API (no key required).
Docs: https://api.multiversx.com/#/

MultiversX has several distinct asset kinds on top of the native EGLD coin:
  - FungibleESDT : a regular fungible token (e.g. USDC, WTAO, MEX, ...)
  - MetaESDT     : a fungible-like token that also carries extra on-chain
                    metadata (typically LP / liquid-staking tokens)
  - NonFungibleESDT (NFT) : one-of-a-kind, amount is always 1, decimals 0
  - SemiFungibleESDT (SFT): fungible within its own nonce, decimals usually 0

Each TokenBalance.asset_type below is set to "esdt", "meta-esdt", "nft" or
"sft" accordingly, so the caller can tell them apart.

EGLD staked through delegation or direct validator staking is held by
dedicated smart contracts, not the wallet itself, so it never shows up in
the plain account balance. This provider also queries:
  - /accounts/{address}/delegation        (stake delegated to staking pools,
    one entry per pool contract, "esdt" asset_type "delegation")
  - /accounts/{address}/delegation-legacy  (old, pre-staking-pools delegation)
  - /accounts/{address}/stake              (direct/validator staking, for
    addresses that run their own validator node)
and reports each as an EGLD-denominated TokenBalance with asset_type
"delegation", "delegation-legacy" or "validator-stake".

Undelegating EGLD (unstaked, but still working through the ~10-day unbonding
period before it can be withdrawn back to the wallet) also lives on the
delegation contract, not the wallet, so it's covered too:
  - "delegation-unbonding"   : still cooling down (userUndelegatedList), name
                                includes the estimated days left
  - "delegation-unbondable"  : cooldown finished, ready for an explicit
                                withdraw transaction (userUnBondable)
"""

from __future__ import annotations

__version__ = "0.1.12"






import re
from urllib.parse import quote

import requests

from .base import BaseProvider, TokenBalance, WalletBalance
from .net import request_json
from .safe import clean_text, safe_decimals, safe_error

MVX_API = "https://api.multiversx.com"
PAGE_SIZE = 100  # max allowed by the public API
#: hard stop for pagination (a hostile/buggy API repeating full pages would
#: otherwise loop forever): 20 pages x 100 = 2000 assets per endpoint.
MAX_PAGES = 20
_NUM_ERR = (ValueError, TypeError, OverflowError)


def _get(path: str):
    return request_json("GET", f"{MVX_API}{path}")


def _dicts(value) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

_MVX_RE = re.compile(r"^erd1[a-z0-9]{58}$")

# Maps the API's "type" field to our normalized asset_type.
_TYPE_MAP = {
    "FungibleESDT": "esdt",
    "MetaESDT": "meta-esdt",
    "NonFungibleESDT": "nft",
    "SemiFungibleESDT": "sft",
}


class MultiversXProvider(BaseProvider):
    chain_id = "multiversx"
    display_name = "MultiversX"
    native_symbol = "EGLD"

    @classmethod
    def matches(cls, address: str) -> bool:
        return bool(_MVX_RE.match(address.strip()))

    truncated = False

    def _paginate(self, path: str) -> list[dict]:
        """GET all pages of an MultiversX list endpoint (size/from pagination)."""
        items: list[dict] = []
        for page in range(MAX_PAGES):
            offset = page * PAGE_SIZE
            batch = request_json(
                "GET", f"{MVX_API}{path}", params={"from": offset, "size": PAGE_SIZE}
            )
            if not isinstance(batch, list) or not batch:
                break
            items.extend(_dicts(batch))
            if len(batch) < PAGE_SIZE:
                break
        else:
            self.truncated = True  # hit MAX_PAGES with every page full
        return items

    def _to_token_balance(self, tok: dict) -> TokenBalance | None:
        try:
            decimals = safe_decimals(tok.get("decimals", 0))
            if decimals is None:
                return None  # hostile/absurd metadata: ignore the asset
            raw_balance = int(tok.get("balance", 0))
            amount = raw_balance / (10**decimals) if decimals else float(raw_balance)
        except _NUM_ERR:
            return None
        if not amount > 0:  # also rejects NaN
            return None
        api_type = tok.get("type", "FungibleESDT")
        return TokenBalance(
            symbol=clean_text(tok.get("ticker", tok.get("identifier", "?")), 40) or "?",
            name=clean_text(tok.get("name", "Unknown token"), 120) or "Unknown token",
            amount=amount,
            contract=clean_text(tok.get("identifier"), 80) or None,
            asset_type=_TYPE_MAP.get(api_type, "esdt"),
        )

    def get_balance(self, address: str) -> WalletBalance:
        address = address.strip()
        qaddr = quote(address, safe="")
        wallet = WalletBalance(
            chain=self.chain_id, address=address, native_symbol=self.native_symbol
        )

        try:
            data = _get(f"/accounts/{quote(address, safe='')}")
            wallet.native_amount = int(data.get("balance", 0)) / 1e18
        except requests.RequestException as exc:
            wallet.error = f"API error: {safe_error(exc)}"
            return wallet
        except (*_NUM_ERR, AttributeError) as exc:
            wallet.error = f"Unexpected response: {safe_error(exc)}"
            return wallet

        warnings: list[str] = []

        # Fungible + MetaESDT tokens (paginated: an account can hold more
        # than the API's default page size, which is how tokens go missing).
        try:
            fungible = self._paginate(f"/accounts/{qaddr}/tokens")
            for tok in fungible:
                tb = self._to_token_balance(tok)
                if tb:
                    wallet.tokens.append(tb)
        except requests.RequestException as exc:
            warnings.append(f"could not fetch ESDT/MetaESDT tokens: {safe_error(exc)}")

        # NFTs and SFTs live on a separate endpoint.
        try:
            nfts_sfts = self._paginate(f"/accounts/{qaddr}/nfts")
            for tok in nfts_sfts:
                tb = self._to_token_balance(tok)
                if tb:
                    wallet.tokens.append(tb)
        except requests.RequestException as exc:
            warnings.append(f"could not fetch NFTs/SFTs: {safe_error(exc)}")

        # --- Staked EGLD held by staking smart contracts, not the wallet ---

        # Delegation to staking pools (the common way to stake EGLD).
        try:
            for pos in _dicts(_get(f"/accounts/{qaddr}/delegation")):
                try:
                    staked = int(pos.get("userActiveStake", 0)) / 1e18
                except _NUM_ERR:
                    staked = 0
                if staked > 0:
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name="Delegated EGLD (staking pool)",
                            amount=staked,
                            contract=clean_text(pos.get("contract"), 80) or None,
                            asset_type="delegation",
                        )
                    )
                try:
                    rewards = float(pos.get("claimableRewards", 0)) / 1e18
                except _NUM_ERR:
                    rewards = 0
                if rewards > 0:
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name="Claimable delegation rewards",
                            amount=rewards,
                            contract=clean_text(pos.get("contract"), 80) or None,
                            asset_type="delegation-rewards",
                        )
                    )

                # Already unbonded, ready for an explicit withdraw tx.
                try:
                    unbondable = int(pos.get("userUnBondable", 0)) / 1e18
                except _NUM_ERR:
                    unbondable = 0
                if unbondable > 0:
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name="Undelegated EGLD, ready to withdraw",
                            amount=unbondable,
                            contract=clean_text(pos.get("contract"), 80) or None,
                            asset_type="delegation-unbondable",
                        )
                    )

                # Undelegated but still cooling down (unbonding period).
                # Once the cooldown reaches 0, the API keeps the entry in
                # userUndelegatedList (with seconds == 0) *in addition to*
                # moving the same amount into userUnBondable above -- so a
                # matured entry here would double-count money already
                # reported as "delegation-unbondable". Skip anything at or
                # past maturity; only genuinely still-cooling-down amounts
                # are reported here.
                for entry in _dicts(pos.get("userUndelegatedList"))[:200]:
                    try:
                        amount = int(entry.get("amount", 0)) / 1e18
                    except _NUM_ERR:
                        amount = 0
                    if amount <= 0:
                        continue
                    seconds_left = entry.get("seconds")
                    try:
                        seconds_left = int(seconds_left) if seconds_left is not None else None
                    except _NUM_ERR:
                        seconds_left = None
                    if seconds_left is not None and seconds_left <= 0:
                        # Already matured -> already counted via
                        # userUnBondable, don't double-count it here.
                        continue
                    days_left = (
                        round(seconds_left / 86400, 1) if seconds_left is not None else None
                    )
                    name = "Undelegated EGLD, unbonding" + (
                        f" (~{days_left}j restants)" if days_left is not None else ""
                    )
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name=name,
                            amount=amount,
                            contract=clean_text(pos.get("contract"), 80) or None,
                            asset_type="delegation-unbonding",
                        )
                    )
        except requests.RequestException as exc:
            warnings.append(f"could not fetch delegation: {safe_error(exc)}")

        # Legacy (pre-staking-pools) delegation, still used by some addresses.
        try:
            legacy = _get(f"/accounts/{qaddr}/delegation-legacy")
            if not isinstance(legacy, dict):
                legacy = {}
            try:
                legacy_staked = int(legacy.get("userStake", 0)) / 1e18
            except _NUM_ERR:
                legacy_staked = 0
            if legacy_staked > 0:
                wallet.tokens.append(
                    TokenBalance(
                        symbol=self.native_symbol,
                        name="Delegated EGLD (legacy delegation)",
                        amount=legacy_staked,
                        contract="legacy-delegation",
                        asset_type="delegation-legacy",
                    )
                )
            # Best-effort: the legacy delegation endpoint's exact field names
            # for in-progress unbonding are less consistently documented
            # than the staking-pool endpoint above, so this is a defensive
            # attempt rather than a guaranteed match -- it silently yields
            # nothing if the field isn't present, rather than erroring.
            try:
                legacy_undelegated = int(legacy.get("userUnDelegatedValue", 0)) / 1e18
            except _NUM_ERR:
                legacy_undelegated = 0
            if legacy_undelegated > 0:
                wallet.tokens.append(
                    TokenBalance(
                        symbol=self.native_symbol,
                        name="Undelegated EGLD (legacy delegation, unbonding or ready to withdraw)",
                        amount=legacy_undelegated,
                        contract="legacy-delegation",
                        asset_type="delegation-legacy-unbonding",
                    )
                )
        except requests.RequestException as exc:
            warnings.append(f"could not fetch legacy delegation: {safe_error(exc)}")

        # Direct validator staking (running your own node).
        try:
            stake_data = _get(f"/accounts/{qaddr}/stake")
            if not isinstance(stake_data, dict):
                stake_data = {}
            try:
                validator_staked = int(stake_data.get("totalStaked", 0)) / 1e18
            except _NUM_ERR:
                validator_staked = 0
            if validator_staked > 0:
                wallet.tokens.append(
                    TokenBalance(
                        symbol=self.native_symbol,
                        name="Staked EGLD (own validator node)",
                        amount=validator_staked,
                        contract="validator-staking",
                        asset_type="validator-stake",
                    )
                )
        except requests.RequestException as exc:
            warnings.append(f"could not fetch validator stake: {safe_error(exc)}")

        if self.truncated:
            warnings.append(f"asset list truncated at {MAX_PAGES * PAGE_SIZE} entries")

        if warnings:
            wallet.warning = "; ".join(warnings)

        return wallet
