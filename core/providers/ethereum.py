"""Ethereum provider.

Native ETH balance uses a free public JSON-RPC endpoint (no key required).

ERC-20 token balances require an indexer since raw RPC has no "list tokens
held by address" call. If the ETHERSCAN_API_KEY environment variable is set,
this provider uses Etherscan's free `tokentx` endpoint to discover which
ERC-20 contracts the address has ever interacted with, then reads the
*current* balance of each contract directly on-chain via `balanceOf` (so the
amount is always live, not just "ever received"). Without an API key, native
balance still works but tokens are skipped with a warning.

Get a free key at https://etherscan.io/apis

Staked ETH: liquid-staking derivatives (stETH, rETH, cbETH, ...) are plain
ERC-20 tokens sitting in the wallet, so they're already covered by the
token discovery above (once ETHERSCAN_API_KEY is set). What's invisible to
a normal balance/token check is *native* beacon-chain staking: the 32 ETH
per validator moved into the deposit contract, tracked only on the beacon
chain, not the execution-layer account. This provider looks those up via
beaconcha.in's free public API (https://beaconcha.in/api/v1/docs/), which
maps an execution-layer address (as depositor or 0x01 withdrawal
credential) to its beacon validators and their current balances. An
optional BEACONCHAIN_API_KEY raises the (generous) default rate limit.
"""

from __future__ import annotations

__version__ = "0.1.21"






import os
import re

import requests

from .base import BaseProvider, TokenBalance, WalletBalance
from .net import request_json
from .safe import (
    MAX_AMOUNT,
    MAX_ENTRIES,
    clean_text,
    safe_decimals,
    safe_error,
    validate_rpc_url,
)

# Several free public RPC endpoints, tried in order. Public RPCs are
# rate-limited and occasionally flaky, so a single endpoint isn't reliable
# enough on its own. If ETH_RPC_URL is set (and is https), it's tried first.
_DEFAULT_RPCS = [
    "https://eth.llamarpc.com",
    "https://ethereum-rpc.publicnode.com",
    "https://rpc.ankr.com/eth",
    "https://cloudflare-eth.com",
]


def _public_rpcs() -> list[str]:
    # Read lazily (not at import time) so a caller (e.g. a GUI app) can
    # change these env vars at runtime and have it take effect immediately.
    env_rpc = validate_rpc_url(os.environ.get("ETH_RPC_URL"))
    return [env_rpc] + _DEFAULT_RPCS if env_rpc else _DEFAULT_RPCS


ETHERSCAN_API = "https://api.etherscan.io/api"


def _etherscan_key() -> str:
    return os.environ.get("ETHERSCAN_API_KEY", "")


BEACONCHAIN_API = "https://beaconcha.in/api/v1"


def _beacon_key() -> str:
    return os.environ.get("BEACONCHAIN_API_KEY", "")


_ETH_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{1,80}$")

# ERC-20 balanceOf(address) selector
_BALANCE_OF_SELECTOR = "0x70a08231"

#: never query more contracts than this per address (a spam-airdropped
#: address can have thousands; each one costs an RPC call).
MAX_TOKEN_CONTRACTS = 150
_BEACON_CHUNK = 100


def _hex_to_int(raw) -> int | None:
    """Parse a 0x-prefixed hex quantity coming from an RPC; None if invalid."""
    if not isinstance(raw, str) or not _HEX_RE.match(raw):
        return None
    return int(raw, 16)


def _scaled(value: int, decimals: int) -> float:
    """value / 10**decimals without ever overflowing (decimals is clamped
    by the caller, and the result is range-checked by sanitize_wallet)."""
    try:
        return value / (10**decimals)
    except OverflowError:
        return float("inf")  # dropped later by safe_amount


class EthereumProvider(BaseProvider):
    chain_id = "ethereum"
    display_name = "Ethereum"
    native_symbol = "ETH"

    @classmethod
    def matches(cls, address: str) -> bool:
        return bool(_ETH_RE.match(address.strip()))

    truncated_contracts = False

    def _rpc_call(self, method: str, params: list) -> dict:
        """Try each configured RPC endpoint until one returns a valid result."""
        last_error = "no endpoint"
        for endpoint in _public_rpcs():
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                data = request_json(
                    "POST", endpoint, json_body=payload, allow_loopback_http=True
                )
            except requests.RequestException as exc:
                last_error = safe_error(exc)
                continue
            if not isinstance(data, dict):
                last_error = "unexpected response format"
                continue
            if "error" in data:
                last_error = f"RPC error: {safe_error(data['error'])}"
                continue
            if "result" not in data:
                last_error = "unexpected response (no result)"
                continue
            return data
        raise RuntimeError(f"All RPC endpoints failed for {method}. Last error: {last_error}")

    def _get_native_balance(self, address: str) -> float:
        result = self._rpc_call("eth_getBalance", [address, "latest"])
        wei = _hex_to_int(result["result"])
        if wei is None:
            raise ValueError("invalid balance returned by RPC")
        return wei / 1e18

    def _discover_token_contracts(self, address: str) -> dict[str, dict]:
        """Return {contract_address: {"symbol":..., "name":..., "decimals":...}}"""
        params = {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "sort": "desc",
            "apikey": _etherscan_key(),
        }
        data = request_json("GET", ETHERSCAN_API, params=params)
        if not isinstance(data, dict):
            raise requests.RequestException("unexpected Etherscan response")
        result = data.get("result")
        if not isinstance(result, list):
            # Etherscan answers {"status":"0","result":"Invalid API Key"} etc.
            # (an empty history is an empty *list*): surface it, don't hide it.
            raise requests.RequestException(
                f"Etherscan: {clean_text(data.get('message') or result, 120)}"
            )
        contracts: dict[str, dict] = {}
        for tx in result:
            if not isinstance(tx, dict):
                continue
            contract = tx.get("contractAddress")
            # the contract goes into an eth_call: it must be a real address
            if not isinstance(contract, str) or not _ETH_RE.match(contract):
                continue
            if contract in contracts:
                continue
            if len(contracts) >= MAX_TOKEN_CONTRACTS:
                self.truncated_contracts = True
                break
            decimals = safe_decimals(tx.get("tokenDecimal", "18"), default=18)
            contracts[contract] = {
                "symbol": clean_text(tx.get("tokenSymbol"), 40) or "?",
                "name": clean_text(tx.get("tokenName"), 120) or "Unknown token",
                "decimals": decimals,
            }
        return contracts

    def _get_token_balance(self, address: str, contract: str, decimals: int) -> float:
        padded_addr = address.lower().replace("0x", "").rjust(64, "0")
        data = _BALANCE_OF_SELECTOR + padded_addr
        result = self._rpc_call(
            "eth_call", [{"to": contract, "data": data}, "latest"]
        )
        value = _hex_to_int(result.get("result", "0x0"))
        if value is None:
            return 0.0
        return _scaled(value, decimals)

    def _get_beacon_validators(self, address: str) -> list[dict]:
        """Return raw validator dicts (pubkey, index) linked to this address
        as depositor or 0x01 withdrawal-credential recipient."""
        params = {"apikey": _beacon_key()} if _beacon_key() else {}
        data = request_json(
            "GET", f"{BEACONCHAIN_API}/validator/eth1/{address}", params=params
        )
        result = data.get("data", []) if isinstance(data, dict) else []
        if isinstance(result, dict):  # API returns a dict for a single match
            result = [result]
        if not isinstance(result, list):
            return []
        return [v for v in result if isinstance(v, dict)][:MAX_ENTRIES]

    def _get_beacon_balances(self, indices: list[int]) -> list[dict]:
        """Return {publicvalidatorindex/pubkey, balance (Gwei), status} for
        each validator index, batched (chunks of 100, the API maximum)."""
        params = {"apikey": _beacon_key()} if _beacon_key() else {}
        out: list[dict] = []
        for i in range(0, len(indices), _BEACON_CHUNK):
            ids = ",".join(str(x) for x in indices[i : i + _BEACON_CHUNK])
            data = request_json(
                "GET", f"{BEACONCHAIN_API}/validator/{ids}", params=params
            )
            data = data.get("data", []) if isinstance(data, dict) else []
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
                out.extend(v for v in data if isinstance(v, dict))
        return out

    def get_balance(self, address: str) -> WalletBalance:
        address = address.strip()
        wallet = WalletBalance(
            chain=self.chain_id, address=address, native_symbol=self.native_symbol
        )

        try:
            wallet.native_amount = self._get_native_balance(address)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            wallet.error = f"RPC error: {safe_error(exc)}"
            return wallet

        warnings: list[str] = []
        if os.environ.get("ETH_RPC_URL") and not validate_rpc_url(os.environ.get("ETH_RPC_URL")):
            warnings.append("ETH_RPC_URL ignored (must be https)")

        # ERC-20 tokens (needs Etherscan key to discover which contracts to check).
        if not _etherscan_key():
            warnings.append(
                "ETHERSCAN_API_KEY not set: ERC-20 token balances skipped"
            )
        else:
            try:
                contracts = self._discover_token_contracts(address)
            except requests.RequestException as exc:
                warnings.append(f"Etherscan error, tokens skipped: {safe_error(exc)}")
                contracts = {}

            if self.truncated_contracts:
                warnings.append(
                    f"only the {MAX_TOKEN_CONTRACTS} most recent token contracts were checked"
                )
            for contract, meta in contracts.items():
                try:
                    amount = self._get_token_balance(
                        address, contract, meta["decimals"]
                    )
                except (requests.RequestException, RuntimeError):
                    continue
                if amount > 0:
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=meta["symbol"],
                            name=meta["name"],
                            amount=amount,
                            contract=contract,
                        )
                    )

        # Native beacon-chain staking (32-ETH validators), invisible to
        # eth_getBalance / ERC-20 checks since it lives on the beacon chain.
        try:
            validators = self._get_beacon_validators(address)
            indices = [
                v["validatorindex"]
                for v in validators
                if isinstance(v.get("validatorindex"), int)
                and not isinstance(v.get("validatorindex"), bool)
                and 0 <= v["validatorindex"] < 10**9
            ]
            if indices:
                balances = self._get_beacon_balances(indices)
                for v in balances:
                    try:
                        gwei = int(v.get("balance", 0))
                    except (ValueError, TypeError):
                        continue
                    if gwei <= 0:
                        continue
                    if gwei > MAX_AMOUNT:
                        continue
                    idx = clean_text(v.get("validatorindex", "?"), 12)
                    status = clean_text(v.get("status", "unknown"), 30)
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name=f"Staked {self.native_symbol} (validator #{idx}, {status})",
                            amount=gwei / 1e9,
                            contract=clean_text(v.get("pubkey"), 120) or None,
                            asset_type="beacon-stake",
                        )
                    )
        except requests.RequestException as exc:
            warnings.append(f"could not fetch beacon-chain staking: {safe_error(exc)}")

        if warnings:
            wallet.warning = "; ".join(warnings)

        return wallet
