"""Print a saved receipt the way a payments operator would want to read it.

    python scripts/show_evidence.py evidence/authorized-AUTH-20260731-024504.json

Reads only the file. No network, no key, no chain access, so it prints the same thing
every time and a video built on it cannot drift. Every value shown came from a
transaction receipt at the moment it settled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def line(label: str, value: object) -> None:
    print(f"{label:<26} {value}")


def show_authorized(record: dict[str, object]) -> None:
    line("run", record["runId"])
    line("payer", record["payer"])
    line("payer key lives in", record["signerBackend"])
    line("submitted by", f"{record['operator']}  (paid the gas)")
    line("transaction", record["txHash"])
    line("block", record["blockNumber"])
    line("gas paid by the operator", f"{record['gasFeeUsdc']} USDC")
    line("payer transaction count", f"{record['payerTransactionCount']}  (it never broadcast)")
    line("USDC transfers from payer", record["usdcTransfersFromPayer"])
    line("screening", record["denylistScreening"])
    print()
    for entry in record["authorizations"]:  # type: ignore[union-attr]
        assert isinstance(entry, dict)
        print(f"  {int(entry['amountMinor']) / 10**6:.6f} USDC -> {entry['payee']}")
        print(f"     reference   {entry['reference']}")
        print(f"     signed by   {entry['recoveredSigner']}")
        print(f"     nonce       {entry['nonce']}")


def show_run(record: dict[str, object]) -> None:
    line("run", record["runId"])
    line("transaction", record["txHash"])
    line("block", record["blockNumber"])
    line("gas used", record["gasUsed"])
    line("fee", f"{record['feeUsdc']} USDC total, {record['feePerPayeeUsdc']} per payee")
    line("memo events", record["memoEvents"])
    line("USDC transfers", record["usdcTransfers"])
    mandate = record.get("mandate")
    if isinstance(mandate, dict):
        line("mandate", mandate["address"])
        line("anchored digest", mandate["digest"])
        line("matches the local run", mandate["digestMatchesLocalRun"])
        line("post quantum verified", mandate["pqVerified"])
    print()
    for payment in record["payments"]:  # type: ignore[union-attr]
        assert isinstance(payment, dict)
        bind = "binds its transfer" if payment["bindsItsTransfer"] else "UNBOUND"
        print(f"  {int(payment['amountMinor']) / 10**6:.6f} USDC -> {payment['payee']}   {bind}")
        print(f"     {payment['instruction']}")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    record = json.loads(path.read_text())
    print(f"receipt      {path.name}")
    print()
    if "signerBackend" in record:
        show_authorized(record)
    else:
        show_run(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
