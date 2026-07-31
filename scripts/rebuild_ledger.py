"""Rebuild the whole ledger from Arc and prove it, with no database.

    python scripts/rebuild_ledger.py
    python scripts/rebuild_ledger.py --from-block 54500000 --reference INV-AE-4103

There is no key here and no local state. The only inputs are an RPC URL and the mandate
contract address, and everything else, every run, every payee, every purpose code, every
invoice reference and the arithmetic that proves the run settled what it authorized,
comes back out of `eth_getLogs`.

That is the claim worth testing on camera: delete the operator's database and the audit
trail survives, because it was never in the database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sanad.arc import addresses  # noqa: E402
from sanad.chain import ArcClient  # noqa: E402
from sanad.iso20022 import describe  # noqa: E402
from sanad.ledger import Ledger, rebuild  # noqa: E402
from sanad.mandate import mandate_address  # noqa: E402


def short(value: bytes | str, keep: int = 6) -> str:
    text = value if isinstance(value, str) else "0x" + value.hex()
    return f"{text[: 2 + keep]}..{text[-4:]}"


def usdc(minor: int) -> str:
    return f"{minor / 10**addresses.USDC_DECIMALS:.6f} USDC"


def print_run(index: int, run: object) -> None:
    label = getattr(run, "run_id") or short(getattr(run, "run_id_hash"))
    print(f"\nrun {index}  {label}")
    print(f"  payer          {getattr(run, 'payer')}")
    print(f"  authorized     {getattr(run, 'payee_count')} payouts, {usdc(getattr(run, 'total_minor'))}")
    print(f"  settled        {len(getattr(run, 'payments'))} payouts, {usdc(getattr(run, 'settled_minor'))}")
    print(f"  anchored       {short(getattr(run, 'anchored_digest'), 8)}   (read off the mandate)")
    if not getattr(run, "digest_recomputable"):
        verdict = f"rule v{getattr(run, 'digest_rule')}, not recomputable by this build"
    else:
        verdict = "MATCH" if getattr(run, "digest_matches") else "MISMATCH"
    print(f"  recomputed     {short(getattr(run, 'recomputed_digest'), 8)}   from the events alone   {verdict}")
    print(f"  post quantum   {'verified on chain' if getattr(run, 'pq_verified') else 'not offered'}")
    print(f"  tx             {addresses.tx_url(getattr(run, 'tx_hash'))}")
    print()
    for position, payment in enumerate(getattr(run, "payments"), start=1):
        binds = "binds" if payment.verified else "UNBOUND"
        print(f"   {position}. {usdc(payment.amount_minor):>16} -> {payment.payee}   {binds}")
        print(f"      {describe(payment.instruction)}")
    problems = getattr(run, "problems")
    if problems:
        print("\n  problems:")
        for problem in problems:
            print(f"    - {problem}")


def print_views(ledger: Ledger) -> None:
    print("\nby ISO 20022 purpose code")
    for code, (count, value) in ledger.by_purpose().items():
        print(f"  {code}  {count:>3} payment(s)  {usdc(value)}")

    uae = ledger.by_uae_purpose()
    if uae:
        print("\nby CBUAE purpose of payment code, the field a UAE bank requires")
        for code, (count, value) in uae.items():
            print(f"  {code}  {count:>3} payment(s)  {usdc(value)}")

    print("\nby counterparty, which is what a lender reads as payment history")
    for payee, (count, value) in ledger.by_counterparty().items():
        print(f"  {payee}  {count:>3} payment(s)  {usdc(value)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-block", type=int, default=None)
    parser.add_argument("--to-block", default="latest")
    parser.add_argument("--reference", default=None, help="look one payment up by invoice reference")
    parser.add_argument("--json", action="store_true", help="write the rebuild to evidence/")
    args = parser.parse_args()

    client = ArcClient()  # no key. A rebuild is a read.
    mandate = mandate_address()
    record = json.loads((ROOT / "deployments" / "arc-testnet.json").read_text())
    deploy_block = record["contracts"]["SanadMandate"].get("deployBlock")
    latest = int(client.w3.eth.block_number)
    from_block = args.from_block if args.from_block is not None else (deploy_block or latest - 10_000)

    print("Rebuilding the Sanad ledger from Arc. No database, no API, no operator.")
    print(f"  chain        {client.chain_id} (Arc testnet)")
    print(f"  mandate      {mandate}")
    print(f"  blocks       {from_block}..{latest}")
    print(f"  signer       {'none, this is a read' if not client.can_sign else client.address}")
    print("\nthree log queries: MandateOpened, Memo, USDC Transfer. Everything else is arithmetic.")

    ledger = rebuild(client, mandate_address=mandate, from_block=from_block, to_block=args.to_block)

    if not ledger.runs:
        print("\nno runs in that range")
        return 0

    for index, run in enumerate(ledger.runs, start=1):
        print_run(index, run)

    print_views(ledger)

    if args.reference:
        print(f"\nlookup {args.reference}, one indexed topic and no scan")
        found = ledger.find_by_reference(args.reference)
        if found is None:
            print("  not found")
        else:
            print(f"  {usdc(found.amount_minor)} to {found.payee} in {short(found.tx_hash, 8)}")
            print(f"  {describe(found.instruction)}")

    print(f"\n{len(ledger.runs)} run(s), {len(ledger.payments)} payment(s), {usdc(ledger.total_minor)}")
    verdict = "every run reconciles against its own mandate" if ledger.all_runs_reconcile else "SOME RUNS DO NOT RECONCILE"
    print(f"verdict: {verdict}")
    print(f"         {ledger.digest_verified_runs} of {len(ledger.runs)} run(s) had the authorization recomputed and matched")

    if args.json:
        out = ROOT / "evidence" / "ledger-rebuild.json"
        out.write_text(
            json.dumps(
                {
                    "fromBlock": ledger.from_block,
                    "toBlock": ledger.to_block,
                    "mandate": mandate,
                    "runs": [
                        {
                            "runId": run.run_id,
                            "runIdHash": "0x" + run.run_id_hash.hex(),
                            "payer": run.payer,
                            "tx": run.tx_hash,
                            "authorizedPayees": run.payee_count,
                            "authorizedMinor": run.total_minor,
                            "settledMinor": run.settled_minor,
                            "anchoredDigest": "0x" + run.anchored_digest.hex(),
                            "recomputedDigest": "0x" + run.recomputed_digest.hex(),
                            "digestRule": run.digest_rule,
                            "digestRecomputable": run.digest_recomputable,
                            "digestMatches": run.digest_matches,
                            "everyInstructionBinds": run.every_instruction_binds,
                            "pqVerified": run.pq_verified,
                            "complete": run.complete,
                            "problems": run.problems,
                            "payments": [
                                {
                                    "reference": p.reference,
                                    "isoPurpose": p.purpose,
                                    "uaePurpose": p.instruction.uae_purpose,
                                    "payee": p.payee,
                                    "amountMinor": p.amount_minor,
                                    "verified": p.verified,
                                    "memoIndex": p.memo_index,
                                }
                                for p in run.payments
                            ],
                        }
                        for run in ledger.runs
                    ],
                    "byPurpose": {k: list(v) for k, v in ledger.by_purpose().items()},
                    "byUaePurpose": {k: list(v) for k, v in ledger.by_uae_purpose().items()},
                    "allRunsReconcile": ledger.all_runs_reconcile,
                },
                indent=1,
            )
            + "\n"
        )
        print(f"evidence     {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
