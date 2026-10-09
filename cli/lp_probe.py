#!/usr/bin/env python3
"""Diagnose how an LP token can be valued from its pool contract.

    python lp_probe.py ALP-2975f6
    python lp_probe.py ALP-2975f6 --mvx-gateway https://my.node

Prints the pool contract behind the LP token, what it holds, which adapter
(if any) recognises it, and the answer of ~30 common view names (zero
argument and keyed by the LP id). If no adapter matches, paste this output
when asking for a new adapter, or add a dict to ADAPTERS in providers/lp.py
using the view names that answer here (see README, "LP tokens").
"""

from __future__ import annotations

__version__ = "0.1.22"






import os as _os
import sys as _sys

_CORE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "core")
if _os.path.isdir(_CORE):  # shared code (providers/, pricing.py) lives in ../core
    _sys.path.insert(0, _CORE)

import argparse
import os
import re
import sys

import requests

from providers import lp
from providers.net import request_json
from providers.safe import clean_text, safe_error, validate_rpc_url


def _leb(buf: bytes, pos: int):
    result = shift = 0
    while True:
        if pos >= len(buf) or shift > 35:
            raise ValueError("bad LEB128")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def wasm_exports(code: bytes) -> list[str]:
    """Exported function names of a WebAssembly module (the contract's endpoints
    and views: a contract publishes no ABI, but its bytecode lists them)."""
    if code[:4] != b"\0asm":
        raise ValueError("not a WebAssembly module")
    pos = 8
    while pos < len(code):
        section_id = code[pos]
        size, pos = _leb(code, pos + 1)
        end = pos + size
        if end > len(code):
            raise ValueError("truncated section")
        if section_id == 7:
            count, p = _leb(code, pos)
            if count > 5000:
                raise ValueError("implausible export count")
            out = []
            for _ in range(count):
                n, p = _leb(code, p)
                if n > 1024 or p + n + 1 > end:
                    raise ValueError("bad export entry")
                name = code[p:p + n].decode("utf-8", "replace")
                p += n
                kind = code[p]
                _idx, p = _leb(code, p + 1)
                if kind == 0:
                    out.append(name)
            return out
        pos = end
    return []


def contract_views(sc: str) -> list[str]:
    acc = request_json("GET", f"{lp.MVX_API}/accounts/{sc}", max_bytes=20_000_000)
    code = bytes.fromhex(acc.get("code") or "") if isinstance(acc, dict) else b""
    names = sorted(set(wasm_exports(code))) if code else []
    return [n for n in names if re.match(r"^(get|view|is)[A-Za-z0-9_]*$", n)]


def show(raw: bytes) -> str:
    tid = lp.as_token_id(raw)
    if tid:
        return f"token {tid}"
    n = lp.as_uint(raw)
    if n is not None and len(raw) <= 16:
        return f"uint {n}"
    if n is not None:
        return f"uint {n} (~{n / 1e18:.6g} @18d)"
    return "0x" + raw[:24].hex() + ("…" if len(raw) > 24 else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lp", help="LP token identifier, e.g. ALP-2975f6")
    ap.add_argument("--mvx-gateway", default=None, help="https node/gateway (default: public)")
    ap.add_argument("--trust", action="store_true",
                    help="if an adapter MATCHES, remember this contract's code hash as approved "
                    "(stored in the LP cache file); the app/CLI then treat every pool with the "
                    "same code as verified. Only approve contracts you know are a real DEX pool.")
    args = ap.parse_args()
    if args.mvx_gateway:
        if not validate_rpc_url(args.mvx_gateway):
            print("--mvx-gateway must be https://", file=sys.stderr)
            return 1
        os.environ["MULTIVERSX_GATEWAY_URL"] = args.mvx_gateway
    if not lp._TOKEN_ID_RE.fullmatch(args.lp):
        # a bare ticker (as the app shows it): look the full identifier up
        ticker = args.lp.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,10}", ticker):
            print("Not a token identifier (expected TICKER-abcdef or a TICKER).", file=sys.stderr)
            return 1
        try:
            found = request_json("GET", f"{lp.MVX_API}/tokens", params={"search": ticker, "size": 25})
        except requests.RequestException as exc:
            print("API error:", safe_error(exc))
            return 3
        ids = sorted({t["identifier"] for t in found if isinstance(t, dict)
                      and isinstance(t.get("identifier"), str)
                      and t["identifier"].startswith(ticker + "-")
                      and lp._TOKEN_ID_RE.fullmatch(t["identifier"])}) if isinstance(found, list) else []
        if not ids:
            print(f"No token found with ticker {clean_text(ticker, 12)}.")
            return 2
        if len(ids) > 1:
            print("Several tokens share this ticker; run again with the full identifier:")
            for i in ids:
                print("  ", i)
            return 2
        args.lp = ids[0]
        print(f"ticker {ticker} -> {args.lp}")

    from providers import version as _version
    cache_file = os.environ.get("WALLET_LP_CACHE") or lp.default_cache_path()
    lp.set_cache_path(cache_file)   # read the approvals; written only with --trust
    print("components:", _version.summary({"lp_probe": __version__}))
    print(f"gateway: {lp.gateway_url().split('//')[0]}//{lp.gateway_url().split('//')[1].split('/')[0]}")
    try:
        facts = lp._token_facts(args.lp)
        if not facts:
            print("Token not found or inconsistent supply: cannot be valued from a pool.")
            return 2
        cands = lp.candidate_contracts(args.lp, facts)
    except requests.RequestException as exc:
        print("API error:", safe_error(exc))
        return 3
    print(f"issuer        : {facts['owner']}")
    print(f"LP supply     : {facts['supply_raw'] / 10 ** facts['decimals']:.8g}")
    print(f"candidate pool contracts (mint/burn role holders, then issuer): {len(cands)}")
    for c in cands:
        print("   ", c)

    budget = lp.Budget(400)
    print("\n--- adapters ---")
    try:
        res = lp.discover_pool(args.lp, budget, {})
    except (requests.RequestException, ValueError) as exc:
        res = None
        print("error:", safe_error(exc))
    if res:
        st = res[1]
        print(f"MATCH: {st.adapter} ({st.curve}), trust: {st.trust or 'NOT VERIFIED'}")
        if args.trust:
            if st.trust == "known":
                print("code hash already known: nothing to approve")
            elif st.code_hash and lp.get_cache().add_trusted(st.code_hash, st.adapter):
                lp.get_cache().save()
                print(f"approved code hash {st.code_hash} for {st.adapter} (saved to {lp.get_cache().path})")
            else:
                print("could not approve this contract")
        for tid, raw in st.reserves.items():
            print(f"   reserve {tid}: {raw}")
    else:
        print("no adapter matched (or answers contradicted the chain: not valued)")

    print("adapters in this lp.py:", ", ".join(a["name"] for a in lp.ADAPTERS))
    print("role holders:", sorted(facts.get("role_holders", ())) or "none (issuer only)")
    print("--- adapter diagnostics (why each one did or did not match) ---")
    for sc in cands:
        for spec in lp.ADAPTERS:
            try:
                st = lp._STRATEGY[spec["kind"]](spec, args.lp, facts, sc, {}, lp.Budget(200))
                print(f"   {spec['name']:<16} {'MATCH' if st else 'no (views absent / not this dialect)'}")
            except lp.Inconsistent as exc:
                print(f"   {spec['name']:<16} CONTRADICTED: {exc}")
            except (requests.RequestException, ValueError) as exc:
                print(f"   {spec['name']:<16} error: {safe_error(exc)}")

    for sc in cands:
        print(f"\n--- views answering on {sc} (zero argument, then keyed by the LP id) ---")
        for label, vargs in (("", []), ("[LP id]", [args.lp.encode().hex()])):
            for name in lp.PROBE_NAMES:
                try:
                    out = lp.vm_query(sc, name, vargs, budget)
                except (requests.RequestException, ValueError) as exc:
                    print(f"{name}{label}: error {safe_error(exc)}")
                    continue
                if out:
                    more = f" (+{len(out) - 8} more)" if len(out) > 8 else ""
                    print(f"{name}{label}: " + " | ".join(show(x) for x in out[:8]) + more)
        try:
            h = request_json("GET", f"{lp.MVX_API}/accounts/{sc}", params={"fields": "codeHash"})
            print(f"\ncode hash of {sc}: {clean_text(str(h.get('codeHash')), 60)}")
        except requests.RequestException:
            pass
        print(f"\n--- every exported get/view/is of {sc} (read from its bytecode), zero argument ---")
        try:
            names = contract_views(sc)
        except (requests.RequestException, ValueError) as exc:
            print("bytecode unreadable:", safe_error(exc))
            names = []
        skip = re.compile(r"(?i)(owner|admin|role|whitelist|blacklist|paused|reward|farm|stak|burner|treasury)")
        for name in names[:80]:
            if skip.search(name):
                continue
            try:
                out = lp.vm_query(sc, name, [], budget)
            except (requests.RequestException, ValueError) as exc:
                print(f"{name}: error {safe_error(exc)}")
                continue
            print(f"{name}: " + (" | ".join(show(x) for x in out[:8]) if out else "(needs arguments / no answer)"))
        try:
            held = request_json("GET", f"{lp.MVX_API}/accounts/{sc}/tokens", params={"size": 20})
        except requests.RequestException:
            held = []
        print(f"--- fungible tokens held by {sc} (first 20) ---")
        for t in held if isinstance(held, list) else []:
            if isinstance(t, dict):
                print(f"   {clean_text(str(t.get('identifier')), 30)}  balance {clean_text(str(t.get('balance')), 40)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
