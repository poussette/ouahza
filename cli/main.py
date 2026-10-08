#!/usr/bin/env python3
"""
Ouahza (CLI): check native coin + token balances for a list of
Bitcoin / Ethereum / Solana / MultiversX wallet addresses.

Usage:
    python main.py addresses.txt
    python main.py addresses.txt --output result.json
    python main.py addresses.txt --output result.csv --format csv
    python main.py addresses.txt --workers 8

Input file format (one entry per line):
    <address>                  # chain auto-detected from the address format
    <chain>,<address>          # force a specific chain, e.g. "ethereum,0xabc..."
    [Label]                    # starts/switches to a named group of wallets
    # a line starting with # is a comment and is ignored
    (blank lines are ignored)

A [Label] line groups every address line that follows it (until the next
[Label] or end of file) under that name: each wallet is still fetched and
priced individually, but their totals are also summed into one combined
total per label, shown after the per-wallet report and in --output exports.
The same label can reappear later in the file to add more addresses to an
already-started group. Addresses listed before any [Label] are left
ungrouped (still valued individually, just no combined total for them).
See addresses.example.txt for a worked example.

Supported chains out of the box: bitcoin, ethereum, solana, multiversx.
To add a new chain, see providers/__init__.py and providers/base.py.

Every coin, token and staking position is valued in USD and EUR (via the
free CoinGecko API, no key required) and each wallet's total is printed and
exported. Use --no-price to skip valuation (e.g. for offline use / to avoid
CoinGecko rate limits on a very large address list).

By default, only positions worth at least 1 US cent (0.01 USD) are reported:
- tokens/positions with no known USD/EUR value (illiquid tokens, NFTs/SFTs --
  no floor-price source here) are left out, since an unpriced line can't be
  reconciled into any total anyway;
- tokens/positions valued below 0.01 USD (dust) are left out too, and so is
  the native coin line itself when its value is below 0.01 USD (a native
  balance with no known price is still shown).
Nothing is discarded from the underlying data, just from what gets
displayed/exported, and wallet/label totals always include everything:
  --show-dust       also show priced lines worth less than 0.01 USD
  --show-unpriced   complete list: also show everything with no value
                    (implies --show-dust)
With --no-price, nothing has a value in the first place, so --show-unpriced
is implied (filtering would otherwise hide everything).

Optional environment variables:
    ETHERSCAN_API_KEY   Free key from https://etherscan.io/apis, needed to
                        list ERC-20 token balances (native ETH balance works
                        without it).
    BEACONCHAIN_API_KEY Free key from https://beaconcha.in/pricing, raises
                        the rate limit for Ethereum beacon-chain staking
                        lookups (works without it too).
    MULTIVERSX_GATEWAY_URL  MultiversX node/gateway (https) used to read pool
                        contracts for LP-token valuation; default: the public one.
    ETH_RPC_URL         Override the default public Ethereum RPC endpoint.
    SOLANA_RPC_URL       Override the default public Solana RPC endpoint.
"""

from __future__ import annotations

__version__ = "0.1.15"






import os as _os
import sys as _sys

_CORE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "core")
if _os.path.isdir(_CORE):  # shared code (providers/, pricing.py) lives in ../core
    _sys.path.insert(0, _CORE)

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from pricing import apply_pricing
from providers import PROVIDERS, detect_provider, get_provider_by_name
from providers.base import WalletBalance
from providers.safe import (
    MAX_ENTRIES,
    MAX_LINE,
    clean_text,
    csv_text,
    safe_error,
    sanitize_wallet,
    validate_rpc_url,
)

MAX_WORKERS = 16


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}"


def parse_input_file(path: Path) -> list[tuple[str | None, str | None, str]]:
    """Return a list of (label_or_None, forced_chain_or_None, address) tuples.

    A line of the form "[Some Label]" sets the current label for every
    address line that follows, until the next such line or EOF. It doesn't
    itself produce an entry.
    """
    entries: list[tuple[str | None, str | None, str]] = []
    current_label: str | None = None
    # "utf-8-sig" (not plain "utf-8") so that a leading UTF-8 BOM -- common
    # in files saved by Notepad/Excel on Windows -- gets stripped instead of
    # silently staying glued to the first line as an invisible character.
    # Left in place, it makes "[Label]" fail its own startswith("[") check
    # (the line actually starts with the BOM), so the label line falls
    # through to being parsed as a bogus address on the *first* label
    # of the file specifically -- a confusing, position-dependent bug.
    with path.open(encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()[:MAX_LINE]
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]") and len(line) > 2:
                current_label = clean_text(line[1:-1], 100) or None
                continue
            if "," in line:
                chain, address = line.split(",", 1)
                entries.append((current_label, chain.strip().lower(), address.strip()))
            else:
                entries.append((current_label, None, line))
            if len(entries) > MAX_ENTRIES:
                raise ValueError(
                    f"Too many addresses (max {MAX_ENTRIES}); split the file."
                )
    return entries


def _finish(wallet: WalletBalance, label: str | None) -> WalletBalance:
    wallet.label = label
    sanitize_wallet(wallet)  # printable text, no secrets, sane numbers
    return wallet


def resolve_wallet(label: str | None, forced_chain: str | None, address: str) -> WalletBalance:
    if forced_chain:
        provider_cls = get_provider_by_name(forced_chain)
        if provider_cls is None:
            known = ", ".join(p.chain_id for p in PROVIDERS)
            return _finish(
                WalletBalance(
                    chain=forced_chain,
                    address=address,
                    native_symbol="?",
                    error=f"Unknown chain '{clean_text(forced_chain, 30)}'. Known chains: {known}",
                ),
                label,
            )
        # A forced chain still has to look like an address of that chain:
        # the value ends up in URLs and RPC payloads.
        if not provider_cls.matches(address):
            return _finish(
                WalletBalance(
                    chain=provider_cls.chain_id,
                    address=address,
                    native_symbol=provider_cls.native_symbol,
                    error=f"Invalid {provider_cls.display_name} address format.",
                ),
                label,
            )
    else:
        provider_cls = detect_provider(address)
        if provider_cls is None:
            return _finish(
                WalletBalance(
                    chain="unknown",
                    address=address,
                    native_symbol="?",
                    error="Could not auto-detect chain from address format.",
                ),
                label,
            )

    provider = provider_cls()
    try:
        wallet = provider.get_balance(address)
    except Exception as exc:  # keep going even if one provider misbehaves
        wallet = WalletBalance(
            chain=provider_cls.chain_id,
            address=address,
            native_symbol=provider_cls.native_symbol,
            error=f"Unexpected error: {safe_error(exc)}",
        )
    return _finish(wallet, label)


def fetch_all(
    entries: list[tuple[str | None, str | None, str]], workers: int
) -> list[WalletBalance]:
    results: list[WalletBalance] = [None] * len(entries)  # type: ignore
    workers = max(1, min(int(workers), MAX_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(resolve_wallet, label, chain, addr): idx
            for idx, (label, chain, addr) in enumerate(entries)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()
    return results


def filter_unpriced(results: list[WalletBalance]) -> None:
    """Mutate `results` in place: drop tokens/positions with no usd_value
    and no eur_value from each wallet's `tokens` list, recording how many
    were dropped in `hidden_unpriced_count`. Totals were already computed
    from the full list before this runs, so they aren't affected -- this
    only changes what gets displayed/exported."""
    for w in results:
        kept = [t for t in w.tokens if t.usd_value is not None or t.eur_value is not None]
        w.hidden_unpriced_count = len(w.tokens) - len(kept)
        w.tokens = kept


#: lines worth less than this many USD are hidden by default (--show-dust
#: brings them back).
DUST_THRESHOLD_USD = 0.01


def filter_dust(results: list[WalletBalance], threshold_usd: float = DUST_THRESHOLD_USD) -> None:
    """Mutate `results` in place: drop tokens/positions whose usd_value is
    below `threshold_usd` and flag the native coin line as hidden when its
    own value is below it. Lines with no usd_value at all are left alone
    (that's filter_unpriced's job, not this one's). Counts go to
    `hidden_dust_count`. As with filter_unpriced, totals were computed from
    the full data beforehand and are unaffected."""
    for w in results:
        kept = [
            t for t in w.tokens
            if t.usd_value is None or t.usd_value >= threshold_usd
        ]
        w.hidden_dust_count = len(w.tokens) - len(kept)
        w.tokens = kept
        if (
            not w.error
            and w.native_usd_value is not None
            and w.native_usd_value < threshold_usd
        ):
            w.native_hidden = True
            w.hidden_dust_count += 1


def compute_label_totals(
    results: list[WalletBalance],
) -> "dict[str, dict[str, float | int]]":
    """Return {label: {"usd": ..., "eur": ..., "wallets": n, "priced": n}},
    in the order labels first appear. Only priced wallets (total_usd is not
    None) contribute to the sums; `wallets` counts all of them, `priced`
    only the ones that contributed."""
    totals: "dict[str, dict[str, float | int]]" = {}
    for w in results:
        if not w.label:
            continue
        bucket = totals.setdefault(
            w.label, {"usd": 0.0, "eur": 0.0, "wallets": 0, "priced": 0}
        )
        bucket["wallets"] += 1
        if not w.error and w.total_usd is not None:
            bucket["usd"] += w.total_usd
            bucket["eur"] += w.total_eur
            bucket["priced"] += 1
    return totals


def print_table(results: list[WalletBalance]) -> None:
    current_label = "__unset__"  # sentinel: no wallet ever has this label
    for wallet in results:
        if wallet.label != current_label:
            current_label = wallet.label
            if current_label:
                print(f"### {current_label} ###")
                print()

        header = f"[{wallet.chain}] {wallet.address}"
        print(header)
        print("-" * len(header))
        if wallet.error:
            print(f"  ERROR: {wallet.error}")
        else:
            if not wallet.native_hidden:
                usd = _fmt_money(wallet.native_usd_value)
                eur = _fmt_money(wallet.native_eur_value)
                print(
                    f"  {wallet.native_symbol}: {wallet.native_amount}"
                    f"   (${usd} / €{eur})"
                )
            if wallet.warning:
                print(f"  (warning: {wallet.warning})")
            if wallet.tokens:
                for tok in wallet.tokens:
                    usd = _fmt_money(tok.usd_value)
                    eur = _fmt_money(tok.eur_value)
                    print(
                        f"  [{tok.asset_type:<28}] {tok.symbol:<10} {tok.amount}"
                        f"   (${usd} / €{eur})  ({tok.name})"
                    )
            elif (
                not wallet.warning
                and not wallet.hidden_unpriced_count
                and not wallet.hidden_dust_count
            ):
                print("  (no tokens found)")
            if wallet.hidden_unpriced_count:
                print(
                    f"  ({wallet.hidden_unpriced_count} position(s) sans valeur "
                    f"connue masquée(s) -- utiliser --show-unpriced pour les voir)"
                )
            if wallet.hidden_dust_count:
                print(
                    f"  ({wallet.hidden_dust_count} ligne(s) valorisée(s) moins de "
                    f"{DUST_THRESHOLD_USD:.2f} $ masquée(s) -- utiliser --show-dust "
                    f"pour les voir)"
                )
            print(
                f"  TOTAL: ${_fmt_money(wallet.total_usd)}"
                f"  /  €{_fmt_money(wallet.total_eur)}"
            )
        print()

    label_totals = compute_label_totals(results)
    if label_totals:
        print("=" * 40)
        print("TOTAUX PAR LABEL")
        print("=" * 40)
        for label, t in label_totals.items():
            if t["priced"] == 0:
                print(f"[{label}] ({t['wallets']} wallet(s)): pas de prix disponible")
                continue
            note = "" if t["priced"] == t["wallets"] else f", {t['priced']}/{t['wallets']} valorisés"
            print(
                f"[{label}] ({t['wallets']} wallet(s){note}): "
                f"${_fmt_money(t['usd'])}  /  €{_fmt_money(t['eur'])}"
            )
        print()

    priced = [w for w in results if not w.error and w.total_usd is not None]
    if priced:
        grand_usd = sum(w.total_usd for w in priced)
        grand_eur = sum(w.total_eur for w in priced)
        print("=" * 40)
        print(
            f"GRAND TOTAL ({len(priced)} wallet(s) priced): "
            f"${_fmt_money(grand_usd)}  /  €{_fmt_money(grand_eur)}"
        )


def _write_private(path: Path, text: str, newline: str | None = None) -> None:
    """Write `text` to `path`, created readable by the owner only (0600):
    results list every wallet you own."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(path, 0o600)  # also tighten a pre-existing file
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)


def write_json(results: list[WalletBalance], path: Path) -> None:
    data = {
        "wallets": [asdict(w) for w in results],
        "label_totals": compute_label_totals(results),
    }
    _write_private(path, json.dumps(data, indent=2, ensure_ascii=False))


def write_csv(results: list[WalletBalance], path: Path) -> None:
    import io

    t = csv_text  # neutralise =,+,-,@ formula injection in text cells
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(
        [
            "label", "chain", "address", "asset_type", "symbol", "name", "amount",
            "usd_value", "eur_value", "contract", "error", "warning",
        ]
    )
    for wallet in results:
        label = t(wallet.label or "")
        chain, addr = t(wallet.chain), t(wallet.address)
        if wallet.error:
            writer.writerow(
                [label, chain, addr, "native", t(wallet.native_symbol), "", "", "", "", "", t(wallet.error), ""]
            )
            continue
        if not wallet.native_hidden:
            writer.writerow(
                [
                    label, chain, addr, "native", t(wallet.native_symbol), "",
                    wallet.native_amount, wallet.native_usd_value, wallet.native_eur_value,
                    "", "", t(wallet.warning or ""),
                ]
            )
        for tok in wallet.tokens:
            writer.writerow(
                [
                    label, chain, addr, t(tok.asset_type), t(tok.symbol), t(tok.name),
                    tok.amount, tok.usd_value, tok.eur_value, t(tok.contract or ""), "", "",
                ]
            )
        writer.writerow(
            [label, chain, addr, "TOTAL", "", "", "", wallet.total_usd, wallet.total_eur, "", "", ""]
        )

    label_totals = compute_label_totals(results)
    if label_totals:
        writer.writerow([])
        writer.writerow(["label", "", "", "LABEL_TOTAL", "", "", "", "usd_value", "eur_value", "", "", ""])
        for label, tot in label_totals.items():
            writer.writerow(
                [t(label), "", "", "LABEL_TOTAL", "", "", "", tot["usd"], tot["eur"], "", "", ""]
            )
    _write_private(path, buf.getvalue(), newline="")


def main() -> int:
    if "--version" in sys.argv[1:]:
        from providers import version as _version
        for name, v in sorted(_version.collect({"main": __version__}).items()):
            print(f"{name:<22} {v or '?'}{'' if v == _version.VERSION else '   <-- DIFFERENT'}")
        print(_version.summary({"main": __version__}))
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_file", type=Path, help="Text file listing wallet addresses (see format above)")
    parser.add_argument("--output", type=Path, default=None, help="Write results to this file (json or csv)")
    parser.add_argument("--format", choices=["json", "csv"], default=None, help="Output format (inferred from --output extension if omitted)")
    parser.add_argument("--workers", type=int, default=6, help=f"Number of concurrent lookups (default: 6, max {MAX_WORKERS})")
    parser.add_argument("--no-price", action="store_true", help="Skip USD/EUR valuation (no CoinGecko calls)")
    parser.add_argument(
        "--show-dust",
        action="store_true",
        help=f"Also report priced coins/tokens/positions worth less than "
        f"{DUST_THRESHOLD_USD:.2f} USD; hidden by default",
    )
    parser.add_argument(
        "--show-unpriced",
        action="store_true",
        help="Complete list: also report tokens/positions with no known "
        "USD/EUR value (illiquid tokens, NFTs/SFTs...), on top of "
        "--show-dust (implied); hidden by default",
    )
    parser.add_argument(
        "--mvx-gateway",
        metavar="URL",
        default=None,
        help="MultiversX node/gateway used to read pool contracts (LP token "
        "valuation); https only. Default: the public gateway "
        "(env MULTIVERSX_GATEWAY_URL also works)",
    )
    parser.add_argument(
        "--no-lp",
        action="store_true",
        help="Do not value MultiversX LP tokens from pool contracts (faster)",
    )
    parser.add_argument(
        "--lp-cache",
        metavar="FILE",
        default=None,
        help="Where to remember which contract is the pool of each LP token "
        "(public data only; speeds up later runs). Default: "
        "~/.ouahza/lp_cache.json (env WALLET_LP_CACHE also works). "
        "Use --lp-cache none to disable",
    )
    parser.add_argument(
        "--lp-trust-all",
        action="store_true",
        help="Permissive mode: LP pools whose contract code is not a known one are "
        "valued like verified ones if they pass every consistency check (including "
        "the 50/50 estimate). Less safe: anyone can deploy a contract that imitates a pool. "
        "Prefer approving a pool once: lp_probe.py <LP> --trust",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the release number of every component file and exit "
        "(spots files left over from an older release after a patch)",
    )
    args = parser.parse_args()
    if args.lp_trust_all:
        os.environ["WALLET_LP_TRUST_ALL"] = "1"
    from providers import lp as _lp_default
    cache_arg = args.lp_cache or os.environ.get("WALLET_LP_CACHE") or _lp_default.default_cache_path()
    from providers import lp as _lp_module
    _lp_module.set_cache_path(None if cache_arg.lower() == "none" else cache_arg)
    if args.mvx_gateway:
        if not validate_rpc_url(args.mvx_gateway):
            print("--mvx-gateway must be an https:// URL.", file=sys.stderr)
            return 1
        os.environ["MULTIVERSX_GATEWAY_URL"] = args.mvx_gateway
    if args.no_lp:
        os.environ["WALLET_LP_PRICING"] = "0"

    if not args.input_file.exists():
        print(f"Input file not found: {args.input_file}", file=sys.stderr)
        return 1

    try:
        entries = parse_input_file(args.input_file)
    except (ValueError, OSError) as exc:
        print(f"Cannot read input file: {safe_error(exc)}", file=sys.stderr)
        return 1
    if not entries:
        print("No addresses found in input file.", file=sys.stderr)
        return 1

    results = fetch_all(entries, workers=args.workers)

    priced_ok = True
    if not args.no_price:
        try:
            shown = []

            def _lp_progress(done, total, ok):
                shown.append(1)
                print(f"\rLP : {done}/{total} examiné(s), {ok} valorisé(s)", end="", file=sys.stderr)

            priced_ok = apply_pricing(results, on_progress=_lp_progress) is not False
            if shown:
                print(file=sys.stderr)
        except Exception as exc:  # pricing is best-effort: keep raw balances
            priced_ok = False
            print(f"Pricing failed: {safe_error(exc)}", file=sys.stderr)
        import pricing as _pricing
        for note in _pricing.LAST_NOTES:
            print(note, file=sys.stderr)
        if not priced_ok:
            print(
                "Prix indisponibles (CoinGecko ?) : soldes bruts affichés, "
                "aucune ligne masquée.",
                file=sys.stderr,
            )

    # Filtering to "priced only" is meaningless once --no-price skipped
    # valuation entirely (it would hide everything), so --show-unpriced is
    # implied in that case. --show-unpriced is the "everything" switch, so it
    # also skips the dust filter; --show-dust alone keeps hiding unpriced
    # lines but lets sub-cent priced ones through.
    if not args.show_unpriced and not args.no_price and priced_ok:
        filter_unpriced(results)
        if not args.show_dust:
            filter_dust(results)

    # Always show the table on screen.
    print_table(results)

    if args.output:
        fmt = args.format or (
            "csv" if args.output.suffix.lower() == ".csv" else "json"
        )
        if fmt == "csv":
            write_csv(results, args.output)
        else:
            write_json(results, args.output)
        print(f"Results written to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
