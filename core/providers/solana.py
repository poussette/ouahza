"""Solana provider, backed by the public mainnet-beta JSON-RPC endpoint.

No API key required. SPL token symbols/names are resolved via Jupiter's
public token list (best-effort, cached in-process); unknown mints just show
their mint address as the symbol.

Native SOL staked through the built-in Stake Program lives in separate
"stake accounts" owned by the Stake program, not in the wallet's own
balance, so it's invisible to a plain getBalance call. This provider looks
those up with getProgramAccounts filtered to stake accounts where the
wallet address is the "staker" authority (the one that controls the
delegation -- the same authority a wallet app uses to show "your" stake).
It does NOT additionally require the wallet to also be the "withdrawer"
authority: plenty of legitimate personal setups use a different withdraw
authority (e.g. a hardware wallet key kept separate from the hot wallet
used to manage delegation), and requiring both would silently drop those
real positions. Each entry's name includes the withdraw authority so you
can see when it differs from the wallet address and double-check who
actually controls redemption of that stake.

(This does mean that if this address is ever used as a *pooled* staker
authority managing delegation for other people's stake accounts, e.g. as
part of a staking service, all of those would be picked up too. That's a
real limitation with no fully reliable fix from public on-chain data alone
-- see "Limites connues" in the README.)

A stake account only earns rewards while actively delegated. Once
deactivated (fully unstaked, waiting to be withdrawn back to the wallet)
or mid-way through deactivating, its SOL is still sitting in the stake
account (not the wallet) but is no longer "at stake". This provider tells
the two apart using the account's activation/deactivation epoch versus the
cluster's current epoch, and tags them accordingly:
  - "staked"            : currently active or activating
  - "stake-withdrawable" : deactivating or fully deactivated, not earning
                            rewards anymore, ready (or soon ready) to be
                            withdrawn back to the wallet as liquid SOL

The reported amount is the stake account's actual current lamport balance
(not the on-chain `delegation.stake` figure), because that figure can stay
frozen at the historical delegated amount even after the SOL has already
been withdrawn back out. A fully-unstaked-and-withdrawn account is usually
left open on-chain holding only its rent-exempt reserve (~0.00228 SOL);
such dust balances are filtered out below so old, already-withdrawn stake
accounts don't show up as if they still held real money.

(Liquid-staking derivatives such as mSOL/jitoSOL are regular SPL tokens and
are already picked up by the normal token scan above.)
"""

from __future__ import annotations

__version__ = "0.1.3"






import os
import re

import requests

from .base import BaseProvider, TokenBalance, WalletBalance
from .net import request_json
from .safe import MAX_ENTRIES, clean_text, safe_amount, safe_error, validate_rpc_url

_DEFAULT_SOLANA_RPC = "https://api.mainnet-beta.solana.com"


def _public_rpc() -> str:
    # Read lazily (not at import time) so a caller (e.g. a GUI app) can
    # change SOLANA_RPC_URL at runtime, after this module has already been
    # imported, and have it take effect on the next lookup.
    return validate_rpc_url(os.environ.get("SOLANA_RPC_URL")) or _DEFAULT_SOLANA_RPC
JUPITER_TOKEN_LIST = "https://token.jup.ag/all"
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
STAKE_PROGRAM_ID = "Stake11111111111111111111111111111111111111"
# Stake account layout: 4-byte enum tag + 8-byte rent_exempt_reserve, then
# Authorized{staker: Pubkey, withdrawer: Pubkey}. So `staker` starts at byte
# offset 12 and `withdrawer` right after it, at offset 44, within the
# (fixed-size, 200-byte) account data.
_STAKE_ACCOUNT_SIZE = 200
_STAKER_OFFSET = 12
# Sentinel used by the protocol to mean "never deactivated".
_NEVER_DEACTIVATED = 2**64 - 1
# A fully-withdrawn stake account is typically left open holding only its
# rent-exempt reserve (~0.00228288 SOL for a 200-byte account). Balances at
# or below this are treated as an emptied/closed-in-practice account, not
# a real position, regardless of what its stale delegation record says.
_DUST_THRESHOLD_LAMPORTS = 5_000_000  # 0.005 SOL, comfortably above rent reserve

# Base58, 32-44 chars, no 0/O/I/l. This is a loose check: Solana pubkeys are
# base58-encoded 32-byte values, so length varies with leading zero bytes.
_SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

_token_list_cache: dict[str, dict] | None = None


def _load_token_list() -> dict[str, dict]:
    global _token_list_cache
    if _token_list_cache is not None:
        return _token_list_cache
    try:
        tokens = request_json("GET", JUPITER_TOKEN_LIST, max_bytes=30_000_000)
        _token_list_cache = {
            t["address"]: t
            for t in tokens
            if isinstance(t, dict) and isinstance(t.get("address"), str)
        }
    except (requests.RequestException, ValueError, KeyError, TypeError):
        _token_list_cache = {}
    return _token_list_cache


class SolanaProvider(BaseProvider):
    chain_id = "solana"
    display_name = "Solana"
    native_symbol = "SOL"

    @classmethod
    def matches(cls, address: str) -> bool:
        address = address.strip()
        # Exclude obvious non-Solana formats already claimed by other chains.
        if address.startswith("0x") or address.startswith("bc1") or address.startswith("erd1"):
            return False
        return bool(_SOL_RE.match(address))

    def _rpc_call(self, method: str, params: list) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        data = request_json(
            "POST", _public_rpc(), json_body=payload, max_bytes=30_000_000,
            allow_loopback_http=True,
        )
        if not isinstance(data, dict):
            raise requests.RequestException("unexpected RPC response format")
        return data

    def _get_current_epoch(self) -> int | None:
        try:
            result = self._rpc_call("getEpochInfo", [])
            return int(result["result"]["epoch"])
        except (requests.RequestException, KeyError, TypeError, ValueError):
            return None

    def get_balance(self, address: str) -> WalletBalance:
        address = address.strip()
        wallet = WalletBalance(
            chain=self.chain_id, address=address, native_symbol=self.native_symbol
        )

        try:
            result = self._rpc_call("getBalance", [address])
            if "error" in result:
                wallet.error = f"RPC error: {safe_error(result['error'])}"
                return wallet
            lamports = int(result["result"]["value"])
            wallet.native_amount = lamports / 1e9
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            wallet.error = f"RPC error: {safe_error(exc)}"
            return wallet

        try:
            token_accounts = self._rpc_call(
                "getTokenAccountsByOwner",
                [
                    address,
                    {"programId": SPL_TOKEN_PROGRAM_ID},
                    {"encoding": "jsonParsed"},
                ],
            )
        except requests.RequestException as exc:
            wallet.warning = f"Could not fetch SPL tokens: {safe_error(exc)}"
            return wallet
        if "error" in token_accounts:
            wallet.warning = f"Could not fetch SPL tokens: {safe_error(token_accounts['error'])}"
            return wallet

        token_list = _load_token_list()
        value_list = (token_accounts.get("result") or {}).get("value", [])
        if not isinstance(value_list, list):
            value_list = []
        if len(value_list) > 2000:
            wallet.warning = "token list truncated at 2000 accounts"
        for entry in value_list[:2000]:
            try:
                info = entry["account"]["data"]["parsed"]["info"]
                mint = info["mint"]
                token_amount = info["tokenAmount"]
                amount = safe_amount(token_amount["uiAmountString"] or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(mint, str) or not _SOL_RE.match(mint):
                continue
            if not amount:
                continue
            meta = token_list.get(mint, {})
            wallet.tokens.append(
                TokenBalance(
                    symbol=clean_text(meta.get("symbol"), 40) or (mint[:6] + "…"),
                    name=clean_text(meta.get("name"), 120) or "Unknown SPL token",
                    amount=amount,
                    contract=mint,
                )
            )

        def _add_warning(msg: str) -> None:
            wallet.warning = (wallet.warning + "; " if wallet.warning else "") + msg

        try:
            stake_result = self._rpc_call(
                "getProgramAccounts",
                [
                    STAKE_PROGRAM_ID,
                    {
                        "encoding": "jsonParsed",
                        "filters": [
                            {"dataSize": _STAKE_ACCOUNT_SIZE},
                            {"memcmp": {"offset": _STAKER_OFFSET, "bytes": address}},
                        ],
                    },
                ],
            )
            if "error" in stake_result:
                _add_warning(f"could not fetch staked SOL: {safe_error(stake_result['error'])}")
            else:
                current_epoch = self._get_current_epoch()
                if current_epoch is None:
                    _add_warning(
                        "could not fetch current epoch: stake accounts shown "
                        "as 'staked' without checking whether they're still active"
                    )

                stake_list = stake_result.get("result", [])
                if not isinstance(stake_list, list):
                    stake_list = []
                for entry in stake_list[:MAX_ENTRIES]:
                    try:
                        parsed = entry["account"]["data"]["parsed"]["info"]
                        stake_info = parsed["stake"]["delegation"]
                        voter = clean_text(stake_info.get("voter", "?"), 60)
                        activation_epoch = int(stake_info.get("activationEpoch", 0))
                        deactivation_epoch = int(
                            stake_info.get("deactivationEpoch", _NEVER_DEACTIVATED)
                        )
                        withdrawer = clean_text(
                            parsed.get("meta", {}).get("authorized", {}).get("withdrawer", "?"), 60
                        )
                        # Ground truth: the account's real current balance,
                        # not the (possibly stale, post-withdrawal) figure
                        # recorded in delegation.stake.
                        lamports = int(entry["account"]["lamports"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if lamports <= _DUST_THRESHOLD_LAMPORTS:
                        # Already unstaked and withdrawn in practice; what's
                        # left is just the rent-exempt reserve, not a
                        # meaningful position.
                        continue

                    stake_account = clean_text(entry.get("pubkey", "?"), 60)

                    if current_epoch is None:
                        # Can't determine state reliably; default to "staked"
                        # but flag it via the warning added above.
                        status, asset_type = "unknown", "staked"
                    elif deactivation_epoch == _NEVER_DEACTIVATED:
                        if activation_epoch >= current_epoch:
                            status, asset_type = "activating", "staked"
                        else:
                            status, asset_type = "active", "staked"
                    elif deactivation_epoch > current_epoch:
                        status, asset_type = "deactivating", "stake-withdrawable"
                    else:
                        status, asset_type = "inactive", "stake-withdrawable"

                    withdrawer_note = (
                        f", withdraw authority: {withdrawer} (DIFFERENT from wallet)"
                        if withdrawer != "?" and withdrawer != address
                        else ""
                    )
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name=(
                                f"{'Staked' if asset_type == 'staked' else 'Unstaked (withdrawable)'} "
                                f"{self.native_symbol} — stake account {stake_account}, "
                                f"validator {voter}, status: {status}{withdrawer_note}"
                            ),
                            amount=lamports / 1e9,
                            contract=stake_account,
                            asset_type=asset_type,
                        )
                    )
        except requests.RequestException as exc:
            _add_warning(f"could not fetch staked SOL: {safe_error(exc)}")

        return wallet
