"""Settle one payout run on Arc, for real, and keep the receipt.

This is the whole product in one file: build a run, authorize it, pay every payee with
its ISO 20022 instruction attached, all in a single transaction, then write the receipt
to `evidence/` so nothing in the writeup has to be taken on trust.

    python scripts/settle_run.py            # dry run, prints the plan and the estimate
    python scripts/settle_run.py --send     # signs and sends

The fixture is a Dubai trading SME paying four overseas counterparties in one run: a
supplier in India, a design contractor in Vietnam, goods from Turkey and a remote
employee in Egypt. Amounts are small because this is testnet, the shape is not.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sanad.arc import addresses  # noqa: E402
from sanad.arc.batch import Call3, decode_aggregate3_result, encode_aggregate3  # noqa: E402
from sanad.arc.denylist import screen  # noqa: E402
from sanad.arc.memo import decode_memo_log, encode_memo_call, encode_transfer  # noqa: E402
from sanad.chain import ArcClient  # noqa: E402
from sanad.iso20022 import (  # noqa: E402
    FxLeg,
    PaymentInstruction,
    StructuredRemittance,
    decode,
    describe,
    document_hash,
)
from sanad.mandate import (  # noqa: E402
    TOPIC_MANDATE_OPENED,
    decode_mandate_opened,
    encode_open,
    encode_run_header,
    mandate_address,
    run_id_hash,
)
from sanad.payouts import Payee, PayoutRun  # noqa: E402

RUN_ID = f"RUN-{dt.datetime.now(tz=dt.timezone.utc):%Y%m%d-%H%M%S}"
TODAY = dt.datetime.now(tz=dt.timezone.utc).date()


def build_run(payer: str) -> PayoutRun:
    def address(raw: str) -> str:
        return Web3.to_checksum_address(raw)

    payees = (
        Payee(
            address("0x6a1b4267921f41f9d5d1facf998da9bb930701c4"),
            12_500,
            PaymentInstruction(
                end_to_end_id="INV-AE-4101",
                purpose="SUPP",
                uae_purpose="GDI",
                creditor_reference="SUP-IN-018",
                remittance=StructuredRemittance("INV-AE-4101", TODAY, 12_500),
                document_sha256=document_hash(b"<Document><pain.001/><id>INV-AE-4101</id></Document>"),
            ),
        ),
        Payee(
            address("0x000000000000000000000000000000000000dead"),
            8_750,
            PaymentInstruction(
                end_to_end_id="INV-AE-4102",
                purpose="SCVE",
                uae_purpose="PMS",
                creditor_reference="CTR-VN-004",
                remittance=StructuredRemittance("INV-AE-4102", TODAY, 8_750),
            ),
        ),
        Payee(
            address("0x000000000000000000000000000000000000beef"),
            15_000,
            PaymentInstruction(
                end_to_end_id="INV-AE-4103",
                purpose="GDDS",
                uae_purpose="GDI",
                creditor_reference="PO-TR-221",
                remittance=StructuredRemittance("INV-AE-4103", TODAY, 15_000),
                # Funded in dirhams, settled in USDC, so the sending leg stays visible.
                fx=FxLeg("AED", 55_095, 272_294_000_000),
            ),
        ),
        Payee(
            address("0x1111111111111111111111111111111111111111"),
            20_000,
            PaymentInstruction(
                end_to_end_id="INV-AE-4104",
                purpose="SALA",
                uae_purpose="SAL",
                creditor_reference="EMP-EG-77",
            ),
        ),
    )
    return PayoutRun(run_id=RUN_ID, payer=payer, payees=payees)


def build_calls(run: PayoutRun) -> list[Call3]:
    """The mandate first, then one memo wrapped transfer per payee.

    The mandate is the only entry with `allow_failure=False`. If the authorization
    cannot be anchored then nothing in the run is authorized, so the whole transaction
    should revert. A single bad payee, by contrast, gets reported and skipped.
    """
    mandate_memo = encode_memo_call(
        target=mandate_address(),
        data=encode_open(run.run_id, run.mandate_digest(), run.total_minor, len(run.payees)),
        memo_id=run_id_hash(run.run_id),
        memo_data=encode_run_header(run),
    )
    return [
        Call3(target=addresses.MEMO, call_data=mandate_memo, allow_failure=False),
        *run.build_calls(allow_failure=True),
    ]


def print_plan(run: PayoutRun, calls: list[Call3], data: bytes) -> None:
    print(f"run          {run.run_id}")
    print(f"payer        {run.payer}")
    print(f"token        {run.token}  (USDC on Arc, also the gas token)")
    print(f"payees       {len(run.payees)}")
    print(f"total        {run.total:.6f} USDC ({run.total_minor} minor units)")
    print(f"digest       0x{run.mandate_digest().hex()}")
    print(f"instruction  {run.memo_byte_count} bytes of ISO 20022 across the run")
    print(f"calldata     {len(data)} bytes, {len(calls)} entries in one aggregate3")
    print()
    for payee in run.payees:
        print(f"  {payee.amount:>9.6f} USDC -> {payee.address}")
        print(f"             {describe(payee.instruction)}")
    print()


def parse_receipt(run: PayoutRun, sent: object) -> dict[str, object]:
    """Turn the receipt into the evidence record, checking the things that matter."""
    logs = list(getattr(sent, "logs"))

    def log_address(log: object) -> str:
        raw = log["address"] if isinstance(log, dict) else log.address  # type: ignore[index]
        return str(raw).lower()

    def topic0(log: object) -> str:
        topics = log["topics"] if isinstance(log, dict) else log.topics  # type: ignore[index]
        first = topics[0]
        return "0x" + (first.hex() if isinstance(first, bytes) else str(first).removeprefix("0x"))

    mandate_logs = [log for log in logs if topic0(log) == TOPIC_MANDATE_OPENED]
    memo_logs = [
        decode_memo_log(log)
        for log in logs
        if log_address(log) == addresses.MEMO.lower() and topic0(log) == addresses.TOPIC_MEMO
    ]
    transfers = [
        log
        for log in logs
        if log_address(log) == addresses.USDC.lower() and topic0(log) == addresses.TOPIC_TRANSFER
    ]

    opened = decode_mandate_opened(mandate_logs[0]) if mandate_logs else None
    payments = [m for m in memo_logs if m.target.lower() == run.token.lower()]
    by_memo_id = {payee.memo_id: payee for payee in run.payees}

    def payment_record(memo: object) -> dict[str, object]:
        payee = by_memo_id.get(getattr(memo, "memo_id"))
        expected = None if payee is None else encode_transfer(payee.address, payee.amount_minor)
        return {
            "memoId": "0x" + getattr(memo, "memo_id").hex(),
            "sender": getattr(memo, "sender"),
            "memoIndex": getattr(memo, "memo_index"),
            "instruction": describe(decode(getattr(memo, "memo"))),
            "payee": None if payee is None else payee.address,
            "amountMinor": None if payee is None else payee.amount_minor,
            # The memo commits to the exact call it describes. Recomputing that call
            # locally and matching callDataHash is what makes the pair unforgeable.
            "bindsItsTransfer": expected is not None and getattr(memo, "binds")(expected),
        }

    return {
        "runId": run.run_id,
        "txHash": getattr(sent, "tx_hash"),
        "explorer": getattr(sent, "url"),
        "blockNumber": getattr(sent, "block_number"),
        "status": getattr(sent, "status"),
        "gasUsed": getattr(sent, "gas_used"),
        "feeUsdc": round(getattr(sent, "fee"), 9),
        "feePerPayeeUsdc": round(getattr(sent, "fee") / len(run.payees), 9),
        "mandate": None
        if opened is None
        else {
            "address": mandate_address(),
            "runIdHash": "0x" + opened.run_id_hash.hex(),
            "payer": opened.payer,
            "digest": "0x" + opened.digest.hex(),
            "digestMatchesLocalRun": opened.digest == run.mandate_digest(),
            "totalMinor": opened.total_minor,
            "payeeCount": opened.payee_count,
            "pqVerified": opened.pq_verified,
        },
        "memoEvents": len(memo_logs),
        "usdcTransfers": len(transfers),
        "payments": [payment_record(m) for m in payments],
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    load_dotenv(Path("/home/asuran/Downloads/hackathon-hq/work/moonwalk/.env"))
    key = os.environ.get("SANAD_PAYER_KEY") or os.environ.get("DEPLOYER_PRIVATE_KEY")
    if not key:
        print("set SANAD_PAYER_KEY or DEPLOYER_PRIVATE_KEY", file=sys.stderr)
        return 2

    client = ArcClient(key)
    run = build_run(client.address)
    calls = build_calls(run)
    data = encode_aggregate3(calls)

    print_plan(run, calls, data)

    screening = screen(client, [payee.address for payee in run.payees])
    print(f"screening    {screening.explain()}")
    if not screening.clean:
        print("refusing to build a run with a denylisted payee", file=sys.stderr)
        return 1

    balance = client.balance_minor()
    print(f"payer balance {balance / 10**6:.6f} USDC")
    if balance < run.total_minor:
        print("not enough USDC to settle this run", file=sys.stderr)
        return 1

    # Simulate first. aggregate3 returns Result[] per entry, so a payee that would fail
    # is visible before any money moves.
    preview = decode_aggregate3_result(client.call(addresses.MULTICALL3_FROM, data))
    for call_index, result in enumerate(preview):
        label = "mandate" if call_index == 0 else run.payees[call_index - 1].instruction.end_to_end_id
        state = "ok" if result.success else f"WOULD FAIL {result.return_data.hex()}"
        print(f"  simulate {label:<14} {state}")
    gas = client.estimate(addresses.MULTICALL3_FROM, data)
    print(f"\nestimate {gas} gas, about {gas * client.w3.eth.gas_price / 10**18:.6f} USDC")

    if "--send" not in sys.argv:
        print("\ndry run. add --send to settle.")
        return 0
    if not all(result.success for result in preview):
        print("refusing to send a run with a failing entry", file=sys.stderr)
        return 1

    sent = client.send(addresses.MULTICALL3_FROM, data, gas=int(gas * 1.25))
    record = parse_receipt(run, sent)
    out = ROOT / "evidence" / f"run-{run.run_id}.json"
    out.write_text(json.dumps(record, indent=1) + "\n")

    print(f"\nsettled  {record['txHash']}")
    print(f"         {record['explorer']}")
    print(f"         block {record['blockNumber']}, {record['gasUsed']} gas, "
          f"{record['feeUsdc']} USDC total, {record['feePerPayeeUsdc']} per payee")
    print(f"         {record['memoEvents']} memo events, {record['usdcTransfers']} USDC transfers")
    mandate = record["mandate"]
    if isinstance(mandate, dict):
        print(f"         mandate digest matches local run: {mandate['digestMatchesLocalRun']}")
    for payment in record["payments"]:  # type: ignore[union-attr]
        assert isinstance(payment, dict)
        print(f"         {payment['memoId'][:18]}.. binds={payment['bindsItsTransfer']} "
              f"{payment['instruction']}")
    print(f"\nevidence {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
