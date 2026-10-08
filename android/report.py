"""Business logic shared by the Kivy app: parsing, fetching, pricing and
formatting. Ported from the original CLI's main.py, with the input file
reader turned into a plain-text reader (the app pastes/edits the same
config format directly instead of pointing at a file on disk)."""

from __future__ import annotations

__version__ = "0.1.5"






import json
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Callable

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
)

MAX_WORKERS = 16


def fmt_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}"


def parse_input_text(text: str) -> list[tuple[str | None, str | None, str]]:
    """Same format/semantics as the CLI's parse_input_file, but reading
    from an in-memory string (the app's saved config) instead of a file."""
    entries: list[tuple[str | None, str | None, str]] = []
    current_label: str | None = None
    for raw_line in text.splitlines():
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
            raise ValueError(f"Trop d'adresses (maximum {MAX_ENTRIES}).")
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
        # A forced chain must still look like an address of that chain: the
        # value is placed in URLs and RPC payloads.
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
    entries: list[tuple[str | None, str | None, str]],
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[WalletBalance]:
    results: list[WalletBalance] = [None] * len(entries)  # type: ignore
    workers = max(1, min(int(workers), MAX_WORKERS))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(resolve_wallet, label, chain, addr): idx
            for idx, (label, chain, addr) in enumerate(entries)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()
            done += 1
            if on_progress:
                on_progress(done, len(entries))
    return results


def filter_unpriced(results: list[WalletBalance]) -> None:
    for w in results:
        kept = [t for t in w.tokens if t.usd_value is not None or t.eur_value is not None]
        w.hidden_unpriced_count = len(w.tokens) - len(kept)
        w.tokens = kept


DUST_THRESHOLD_USD = 0.01


def filter_dust(results: list[WalletBalance], threshold_usd: float = DUST_THRESHOLD_USD) -> None:
    """Hide tokens/positions (and the native coin line) worth less than
    `threshold_usd`. Unpriced entries are left alone (filter_unpriced handles
    them). Totals and JSON data stay intact; only the display skips them."""
    for w in results:
        kept = [t for t in w.tokens if t.usd_value is None or t.usd_value >= threshold_usd]
        w.hidden_dust_count = len(w.tokens) - len(kept)
        w.tokens = kept
        if not w.error and w.native_usd_value is not None and w.native_usd_value < threshold_usd:
            w.native_hidden = True
            w.hidden_dust_count += 1


def compute_label_totals(results: list[WalletBalance]) -> dict[str, dict[str, float | int]]:
    totals: dict[str, dict[str, float | int]] = {}
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


def format_table(results: list[WalletBalance]) -> str:
    """Same layout as the CLI's print_table, but returns a string instead
    of printing (for on-screen display in the app)."""
    out = io.StringIO()
    current_label = "__unset__"
    for wallet in results:
        if wallet.label != current_label:
            current_label = wallet.label
            if current_label:
                out.write(f"### {current_label} ###\n\n")

        header = f"[{wallet.chain}] {wallet.address}"
        out.write(header + "\n")
        out.write("-" * len(header) + "\n")
        if wallet.error:
            out.write(f"  ERROR: {wallet.error}\n")
        else:
            if not wallet.native_hidden:
                usd = fmt_money(wallet.native_usd_value)
                eur = fmt_money(wallet.native_eur_value)
                out.write(
                    f"  {wallet.native_symbol}: {wallet.native_amount}"
                    f"   (${usd} / €{eur})\n"
                )
            if wallet.warning:
                out.write(f"  (warning: {wallet.warning})\n")
            if wallet.tokens:
                for tok in wallet.tokens:
                    usd = fmt_money(tok.usd_value)
                    eur = fmt_money(tok.eur_value)
                    out.write(
                        f"  [{tok.asset_type:<24}] {tok.symbol:<10} {tok.amount}"
                        f"   (${usd} / €{eur})  ({tok.name})\n"
                    )
            elif (not wallet.warning and not wallet.hidden_unpriced_count
                  and not wallet.hidden_dust_count and not wallet.native_hidden):
                out.write("  (aucun token trouve)\n")
            if wallet.hidden_unpriced_count:
                out.write(
                    f"  ({wallet.hidden_unpriced_count} position(s) sans valeur "
                    f"connue masquee(s))\n"
                )
            if wallet.hidden_dust_count:
                out.write(
                    f"  ({wallet.hidden_dust_count} position(s) < 1 centime masquee(s))\n"
                )
            out.write(
                f"  TOTAL: ${fmt_money(wallet.total_usd)}"
                f"  /  €{fmt_money(wallet.total_eur)}\n"
            )
        out.write("\n")

    label_totals = compute_label_totals(results)
    if label_totals:
        out.write("=" * 40 + "\n")
        out.write("TOTAUX PAR LABEL\n")
        out.write("=" * 40 + "\n")
        for label, t in label_totals.items():
            if t["priced"] == 0:
                out.write(f"[{label}] ({t['wallets']} wallet(s)): pas de prix disponible\n")
                continue
            note = "" if t["priced"] == t["wallets"] else f", {t['priced']}/{t['wallets']} valorises"
            out.write(
                f"[{label}] ({t['wallets']} wallet(s){note}): "
                f"${fmt_money(t['usd'])}  /  €{fmt_money(t['eur'])}\n"
            )
        out.write("\n")

    priced = [w for w in results if not w.error and w.total_usd is not None]
    if priced:
        grand_usd = sum(w.total_usd for w in priced)
        grand_eur = sum(w.total_eur for w in priced)
        out.write("=" * 40 + "\n")
        out.write(
            f"GRAND TOTAL ({len(priced)} wallet(s) valorises): "
            f"${fmt_money(grand_usd)}  /  €{fmt_money(grand_eur)}\n"
        )
    return out.getvalue()


def build_json(results: list[WalletBalance]) -> str:
    data = {
        "wallets": [asdict(w) for w in results],
        "label_totals": compute_label_totals(results),
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_csv(results: list[WalletBalance]) -> str:
    t = csv_text  # neutralise =,+,-,@ spreadsheet formula injection
    out = io.StringIO()
    writer = csv.writer(out)
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
            writer.writerow([t(label), "", "", "LABEL_TOTAL", "", "", "", tot["usd"], tot["eur"], "", "", ""])
    return out.getvalue()
