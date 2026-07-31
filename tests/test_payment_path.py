"""Payment path tests, anchored to transactions that are actually on Arc.

Two of these read receipts committed under `evidence/`. That is deliberate: a codec
test can agree with itself forever, so the decoder is also pointed at a real log from a
real transaction and asked to reproduce what the chain recorded.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from sanad.arc import addresses
from sanad.arc.batch import (
    AGGREGATE3_SELECTOR,
    Call3,
    decode_aggregate3_result,
    encode_aggregate3,
    split_into_batches,
)
from sanad.arc.memo import (
    MEMO_SELECTOR,
    TRANSFER_SELECTOR,
    call_data_hash,
    decode_memo_log,
    encode_memo_call,
    encode_transfer,
)
from sanad.iso20022 import PaymentInstruction, StructuredRemittance, decode, memo_id
from sanad.payouts import Payee, PayoutError, PayoutRun

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence"
PAYER = "0xDB6c6340342e71A63cD11Ebac2185204b7777777"
AGENT = "0x6a1b4267921f41f9D5D1FACF998Da9BB930701c4"


def instruction(reference: str, purpose: str = "SUPP") -> PaymentInstruction:
    return PaymentInstruction(end_to_end_id=reference, purpose=purpose)


def test_selectors_match_the_calldata_arc_accepted() -> None:
    # Read back off the transactions in evidence/, not computed twice the same way.
    assert MEMO_SELECTOR.hex() == "c3b2c4f8"
    assert TRANSFER_SELECTOR.hex() == "a9059cbb"
    # aggregate3 keeps the standard Multicall3 selector, so Multicall3 tooling reads it.
    assert AGGREGATE3_SELECTOR.hex() == "82ad56cb"


def test_memo_id_matches_the_batch_that_settled() -> None:
    # First payee of tx 0x15c1f307...077d2, whose Memo event carried this topic.
    assert (
        memo_id("INV-AE-1001").hex()
        == "f71df1fff53705fdb08e16b223f0b8c42eb52c56e804526c1104a5d2af075c65"
    )


def test_decoder_reproduces_the_single_payment_receipt() -> None:
    receipt = json.loads((EVIDENCE / "memo-proof-raw.json").read_text())
    logs = [
        log
        for log in receipt["logs"]
        if log["address"].lower() == addresses.MEMO.lower() and len(log["topics"]) == 4
    ]
    assert len(logs) == 1

    memo_log = decode_memo_log(logs[0])
    assert memo_log.sender.lower() == PAYER.lower()
    assert memo_log.target.lower() == addresses.USDC.lower()
    assert memo_log.memo_id == memo_id("INV-2026-0001")
    assert memo_log.memo_index == 322_342
    assert b'"iso20022":"pacs.008"' in memo_log.memo

    # The memo commits to the exact call it describes, so rebuilding that call locally
    # has to reproduce callDataHash. This is the property that makes an audit trail
    # worth anything: the instruction cannot be re-paired with a different transfer.
    assert memo_log.binds(encode_transfer(AGENT, 10_000))
    assert not memo_log.binds(encode_transfer(AGENT, 10_001))


def test_decoder_reproduces_every_memo_in_the_batch() -> None:
    receipt = json.loads((EVIDENCE / "batch-memo-proof-raw.json").read_text())
    memo_logs = [
        decode_memo_log(log)
        for log in receipt["logs"]
        if log["address"].lower() == addresses.MEMO.lower() and len(log["topics"]) == 4
    ]
    assert len(memo_logs) == 3

    expected = [("INV-AE-1001", "SUPP", 5000), ("INV-AE-1002", "SALA", 3000), ("INV-AE-1003", "TRAD", 2000)]
    for log, (reference, purpose, amount) in zip(memo_logs, expected, strict=True):
        assert log.sender.lower() == PAYER.lower(), "every transfer attributes to the payer"
        assert log.memo_id == memo_id(reference)
        assert f'"purp":"{purpose}"'.encode() in log.memo
        assert f'"invc":"{reference}"'.encode() in log.memo
        assert log.memo_index > 0
        del amount


def test_a_run_builds_one_memo_call_per_payee() -> None:
    run = PayoutRun(
        run_id="RUN-2026-07-31-A",
        payer=PAYER,
        payees=(
            Payee(AGENT, 5_000, instruction("INV-AE-2001")),
            Payee(AGENT, 3_000, instruction("INV-AE-2002", "SALA")),
        ),
    )
    calls = run.build_calls()
    assert [call.target for call in calls] == [addresses.MEMO, addresses.MEMO]
    assert all(call.call_data.startswith(MEMO_SELECTOR) for call in calls)
    assert all(call.allow_failure for call in calls)
    assert run.total_minor == 8_000
    assert run.total == pytest.approx(0.008)

    batch = encode_aggregate3(calls)
    assert batch.startswith(AGGREGATE3_SELECTOR)


def test_the_inner_transfer_is_bound_by_hash() -> None:
    payee = Payee(AGENT, 1_250_000, instruction("INV-AE-3001"))
    inner = encode_transfer(payee.address, payee.amount_minor)
    memo_call = encode_memo_call(
        target=addresses.USDC, data=inner, memo_id=payee.memo_id, memo_data=payee.memo_bytes
    )
    assert call_data_hash(inner) != call_data_hash(encode_transfer(AGENT, 1_250_001))
    assert inner in memo_call, "the inner call is carried verbatim, so it can be replayed"
    assert decode(payee.memo_bytes).end_to_end_id == "INV-AE-3001"


def test_a_run_refuses_a_duplicate_payment_reference() -> None:
    with pytest.raises(PayoutError, match="appears twice"):
        PayoutRun(
            run_id="RUN-A",
            payer=PAYER,
            payees=(
                Payee(AGENT, 1, instruction("INV-DUP")),
                Payee(AGENT, 2, instruction("INV-DUP")),
            ),
        )


def test_a_run_refuses_nonsense() -> None:
    with pytest.raises(PayoutError, match="at least one payee"):
        PayoutRun(run_id="RUN-A", payer=PAYER, payees=())
    with pytest.raises(PayoutError, match="not an address"):
        Payee("nope", 1, instruction("INV-1"))
    with pytest.raises(PayoutError, match="must be positive"):
        Payee(AGENT, 0, instruction("INV-1"))


def test_the_mandate_digest_covers_order_amount_and_reference() -> None:
    def run(payees: tuple[Payee, ...]) -> PayoutRun:
        return PayoutRun(run_id="RUN-A", payer=PAYER, payees=payees)

    a = Payee(AGENT, 1_000, instruction("INV-A"))
    b = Payee(AGENT, 2_000, instruction("INV-B"))
    base = run((a, b)).mandate_digest()
    assert len(base) == 32
    assert base == run((a, b)).mandate_digest(), "same run, same digest"
    assert base != run((b, a)).mandate_digest(), "order is part of the authorization"
    assert base != run((a, Payee(AGENT, 2_001, instruction("INV-B")))).mandate_digest()
    assert base != run((a, Payee(PAYER, 2_000, instruction("INV-B")))).mandate_digest()


def test_batches_are_chopped_to_the_requested_size() -> None:
    calls = [Call3(target=addresses.MEMO, call_data=b"\x00") for _ in range(7)]
    assert [len(chunk) for chunk in split_into_batches(calls, max_per_batch=3)] == [3, 3, 1]
    with pytest.raises(ValueError, match="at least 1"):
        split_into_batches(calls, max_per_batch=0)


def test_aggregate3_results_round_trip() -> None:
    from eth_abi.abi import encode as abi_encode

    encoded = abi_encode(["(bool,bytes)[]"], [[(True, b"\x01"), (False, b"boom")]])
    results = decode_aggregate3_result(encoded)
    assert [r.success for r in results] == [True, False]
    assert results[1].return_data == b"boom"


def test_memo_byte_count_is_reported_so_gas_can_be_predicted() -> None:
    run = PayoutRun(
        run_id="RUN-A",
        payer=PAYER,
        payees=(
            Payee(
                AGENT,
                1_250_000,
                PaymentInstruction(
                    end_to_end_id="INV-AE-1001",
                    purpose="SUPP",
                    creditor_reference="SUP-042",
                    remittance=StructuredRemittance(
                        invoice_number="INV-AE-1001",
                        invoice_date=dt.date(2026, 7, 31),
                        invoice_amount_minor=1_250_000,
                    ),
                ),
            ),
        ),
    )
    assert 40 <= run.memo_byte_count <= 64
