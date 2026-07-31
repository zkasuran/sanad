"""Rebuild the whole payout ledger from Arc, with no database.

This module is the point of the project. Everything else writes payment instructions to
the chain, and this reads them back and proves the result, using nothing but
`eth_getLogs` and arithmetic. Drop the operator's database and every run, every payee,
every purpose code and every invoice reference comes back.

Three properties make the rebuild worth trusting, and each one is checked here rather
than asserted:

1. **The instruction belongs to its payment.** Memo records `callDataHash`, the hash of
   the exact call it wrapped. So pairing a memo with a transfer is not positional
   guesswork: rebuild `transfer(to, value)` from the Transfer event, hash it, and see
   whether the memo committed to it. A memo cannot be re-paired with a different
   transfer after the fact.
2. **The payments match what was authorized.** The mandate anchored a digest over the
   payer, the token, the chain and every payee with its amount and memo id in order.
   All of those are recoverable from the events, so the digest can be recomputed and
   compared. A run that paid someone who was not in the mandate fails this check.
3. **The payer is the payer.** Because the calls went through CallFrom, every USDC
   Transfer carries the payer's own EOA in its `from` field, not a contract. Custody
   never moved, so there is no bridge, pool or operator wallet to take on trust.

Ordering comes from `memoIndex`, which the Memo contract increments globally, so
sorting by it restores the order the run was authorized in.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from eth_utils.crypto import keccak

from .arc import addresses
from .arc.batch import decode_aggregate3_calldata
from .arc.memo import (
    MemoLog,
    decode_memo_call,
    decode_memo_log,
    decode_payment_call,
)
from .chain import ArcClient
from .iso20022 import CodecError, PaymentInstruction, decode
from .mandate import (
    TOPIC_MANDATE_OPENED,
    MandateOpened,
    RunHeader,
    decode_mandate_opened,
    decode_run_header,
)
from .payouts import mandate_digest_from_parts


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    """One payout, reconstructed. `verified` is the callDataHash check."""

    memo_id: bytes
    instruction: PaymentInstruction
    payee: str
    amount_minor: int
    payer: str
    token: str
    tx_hash: str
    block_number: int
    memo_index: int
    verified: bool
    kind: str = "transfer"

    @property
    def amount(self) -> float:
        return self.amount_minor / 10**addresses.USDC_DECIMALS

    @property
    def reference(self) -> str:
        return self.instruction.end_to_end_id

    @property
    def purpose(self) -> str:
        return self.instruction.purpose


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One authorized run and the payments that settled under it."""

    run_id_hash: bytes
    payer: str
    submitter: str
    anchored_digest: bytes
    total_minor: int
    payee_count: int
    pq_verified: bool
    tx_hash: str
    block_number: int
    payments: tuple[PaymentRecord, ...]
    header: RunHeader | None = None
    token: str = addresses.USDC

    @property
    def run_id(self) -> str | None:
        """The human readable run id, when the run header carried one."""
        return None if self.header is None else self.header.run_id

    @property
    def settled_minor(self) -> int:
        return sum(payment.amount_minor for payment in self.payments)

    @property
    def recomputed_digest(self) -> bytes:
        """The digest as the chain data implies it. Equality with `anchored_digest` is
        the whole audit."""
        return mandate_digest_from_parts(
            run_id_hash=self.run_id_hash,
            payer=self.payer,
            token=self.token,
            chain_id=addresses.CHAIN_ID,
            payees=[payment.payee for payment in self.payments],
            amounts=[payment.amount_minor for payment in self.payments],
            memo_ids=[payment.memo_id for payment in self.payments],
        )

    @property
    def digest_matches(self) -> bool:
        return self.recomputed_digest == self.anchored_digest

    @property
    def digest_rule(self) -> int:
        """Which digest rule this run was authorized under.

        Version 1 hashed the run id as text, which meant the digest could not be
        recomputed from chain data alone, because only the keccak hash of the run id
        reaches the chain. Version 2 hashes the hash, so the rebuild is self contained.
        The run header carries the version, so an old run can be reported as
        unrecomputable instead of being falsely reported as a mismatch.
        """
        return 1 if self.header is None else self.header.version

    @property
    def digest_recomputable(self) -> bool:
        return self.digest_rule >= 2

    @property
    def every_instruction_binds(self) -> bool:
        return all(payment.verified for payment in self.payments)

    @property
    def complete(self) -> bool:
        """True when the run paid exactly what it authorized, and can prove it."""
        return (
            len(self.payments) == self.payee_count
            and self.settled_minor == self.total_minor
            and self.every_instruction_binds
            and (self.digest_matches if self.digest_recomputable else True)
        )

    @property
    def problems(self) -> list[str]:
        """Why `complete` is false, in words a reconciler can act on."""
        issues: list[str] = []
        if len(self.payments) != self.payee_count:
            issues.append(
                f"authorized {self.payee_count} payouts, found {len(self.payments)} on chain"
            )
        if self.settled_minor != self.total_minor:
            issues.append(
                f"authorized {self.total_minor} minor units, settled {self.settled_minor}"
            )
        if not self.digest_recomputable:
            issues.append(
                f"authorized under digest rule v{self.digest_rule}, which covered the run "
                "id as text, so this build cannot recompute it from chain data. The "
                "payment level checks below still apply"
            )
        elif not self.digest_matches:
            issues.append(
                "the payments do not hash to the anchored mandate digest, so this run "
                "settled something other than what was authorized"
            )
        for payment in self.payments:
            if not payment.verified:
                issues.append(f"{payment.reference}: memo does not bind its own transfer")
        return issues


@dataclass(frozen=True, slots=True)
class Ledger:
    """Every run found in a block range, plus the views a reconciler and a regulator
    actually ask for."""

    runs: tuple[RunRecord, ...]
    from_block: int
    to_block: int

    @property
    def payments(self) -> tuple[PaymentRecord, ...]:
        return tuple(payment for run in self.runs for payment in run.payments)

    @property
    def total_minor(self) -> int:
        return sum(payment.amount_minor for payment in self.payments)

    @property
    def all_runs_reconcile(self) -> bool:
        return all(run.complete for run in self.runs)

    @property
    def digest_verified_runs(self) -> int:
        """How many runs had their authorization recomputed and matched. This is the
        number that matters, and it is smaller than the run count whenever an older
        digest rule is in the range."""
        return sum(1 for run in self.runs if run.digest_recomputable and run.digest_matches)

    def by_purpose(self) -> dict[str, tuple[int, int]]:
        """Count and value per ISO 20022 purpose code. This is the view a regulator
        wants, and on any other stablecoin rail it does not exist on chain at all."""
        out: dict[str, tuple[int, int]] = {}
        for payment in self.payments:
            count, value = out.get(payment.purpose, (0, 0))
            out[payment.purpose] = (count + 1, value + payment.amount_minor)
        return dict(sorted(out.items(), key=lambda item: -item[1][1]))

    def by_counterparty(self) -> dict[str, tuple[int, int]]:
        """Payment history per payee. Repeat, on time settlement to the same
        counterparty is what a lender reads as credit history."""
        out: dict[str, tuple[int, int]] = {}
        for payment in self.payments:
            count, value = out.get(payment.payee, (0, 0))
            out[payment.payee] = (count + 1, value + payment.amount_minor)
        return dict(sorted(out.items(), key=lambda item: -item[1][1]))

    def by_uae_purpose(self) -> dict[str, tuple[int, int]]:
        """Count and value per CBUAE purpose of payment code. This is the view the UAE
        Central Bank's own reporting is built on, and it is the field a bank mandates and
        a plain USDC transfer has nowhere to put."""
        out: dict[str, tuple[int, int]] = {}
        for payment in self.payments:
            code = payment.instruction.uae_purpose
            if not code:
                continue
            count, value = out.get(code, (0, 0))
            out[code] = (count + 1, value + payment.amount_minor)
        return dict(sorted(out.items(), key=lambda item: -item[1][1]))

    def find_by_reference(self, end_to_end_id: str) -> PaymentRecord | None:
        """Look a payment up by its invoice reference. On chain this is one indexed
        topic lookup, because `memoId` is keccak of the end to end id."""
        wanted = keccak(text=end_to_end_id)
        return next((p for p in self.payments if p.memo_id == wanted), None)

    def counterparty_history(self, payee: str) -> tuple[PaymentRecord, ...]:
        target = payee.lower()
        return tuple(p for p in self.payments if p.payee.lower() == target)


def _tx_hash(log: Any) -> str:
    raw = log["transactionHash"] if isinstance(log, dict) else log.transactionHash
    return raw if isinstance(raw, str) else "0x" + bytes(raw).hex()


def _topic_address(topic: Any) -> str:
    raw = topic.hex() if isinstance(topic, bytes) else str(topic)
    return "0x" + raw[-40:]


def _transfer_value(log: Any) -> int:
    data = log["data"] if isinstance(log, dict) else log.data
    raw = bytes(data) if isinstance(data, bytes) else bytes.fromhex(str(data).removeprefix("0x"))
    return int.from_bytes(raw, "big")


def rebuild(
    client: ArcClient,
    *,
    mandate_address: str,
    from_block: int = 0,
    to_block: int | str = "latest",
    token: str = addresses.USDC,
) -> Ledger:
    """Read the ledger back off Arc.

    Three log queries for any number of runs: the mandates, the memos and the USDC
    transfers. Everything after that is a join in memory, so the cost does not grow
    with the number of runs, only with the block range.
    """
    resolved_to = int(client.w3.eth.block_number) if to_block == "latest" else int(to_block)

    mandate_logs = client.logs(
        address=mandate_address,
        topics=[TOPIC_MANDATE_OPENED],
        from_block=from_block,
        to_block=resolved_to,
    )
    memo_logs = client.logs(
        address=addresses.MEMO,
        topics=[addresses.TOPIC_MEMO],
        from_block=from_block,
        to_block=resolved_to,
    )
    transfer_logs = client.logs(
        address=token,
        topics=[addresses.TOPIC_TRANSFER],
        from_block=from_block,
        to_block=resolved_to,
    )

    memos_by_tx: dict[str, list[MemoLog]] = defaultdict(list)
    for log in memo_logs:
        memos_by_tx[_tx_hash(log)].append(decode_memo_log(log))

    # (from, to, amount) triples per transaction, so a decoded payment call can be
    # checked against a transfer that actually happened rather than assumed.
    transfers_by_tx: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for log in transfer_logs:
        topics = log["topics"] if isinstance(log, dict) else log.topics
        transfers_by_tx[_tx_hash(log)].append(
            (_topic_address(topics[1]), _topic_address(topics[2]), _transfer_value(log))
        )

    runs: list[RunRecord] = []
    for log in mandate_logs:
        opened: MandateOpened = decode_mandate_opened(log)
        tx_hash = _tx_hash(log)
        memo_logs_here = {m.memo_id: m for m in memos_by_tx.get(tx_hash, [])}
        transfers_here = transfers_by_tx.get(tx_hash, [])

        # The transaction input carries every inner call verbatim, which is what lets the
        # rebuild check the commitment a memo made instead of trusting the pairing. This
        # is still chain data, it is just data that lives in the transaction rather than
        # in a log.
        inner_calls: list[bytes] = []
        try:
            raw_input = client.w3.eth.get_transaction(tx_hash)["input"]  # type: ignore[arg-type,index]
            calldata = bytes(raw_input) if isinstance(raw_input, bytes) else bytes.fromhex(
                str(raw_input).removeprefix("0x")
            )
            inner_calls = [
                call.call_data
                for call in decode_aggregate3_calldata(calldata)
                if call.target.lower() == addresses.MEMO.lower()
            ]
        except (ValueError, KeyError):
            inner_calls = []

        header: RunHeader | None = None
        payments: list[PaymentRecord] = []
        for entry in inner_calls:
            try:
                memo_call = decode_memo_call(entry)
            except ValueError:
                continue

            memo_log = memo_logs_here.get(memo_call.memo_id)
            if memo_log is None:
                continue

            if memo_call.memo_id == opened.run_id_hash:
                try:
                    header = decode_run_header(memo_call.memo_data)
                except CodecError:
                    header = None
                continue

            if memo_call.target.lower() != token.lower():
                continue
            try:
                instruction = decode(memo_call.memo_data)
                payment = decode_payment_call(memo_call.data)
            except (CodecError, ValueError):
                continue

            payer = payment.payer or memo_log.sender
            binds = memo_log.binds(memo_call.data)
            settled = any(
                sender.lower() == payer.lower()
                and recipient.lower() == payment.payee.lower()
                and value == payment.amount_minor
                for sender, recipient, value in transfers_here
            )
            payments.append(
                PaymentRecord(
                    memo_id=memo_call.memo_id,
                    instruction=instruction,
                    payee=payment.payee,
                    amount_minor=payment.amount_minor,
                    payer=payer,
                    token=token,
                    tx_hash=memo_log.tx_hash,
                    block_number=memo_log.block_number,
                    memo_index=memo_log.memo_index,
                    verified=binds and settled,
                    kind=payment.kind,
                )
            )

        payments.sort(key=lambda record: record.memo_index)
        runs.append(
            RunRecord(
                run_id_hash=opened.run_id_hash,
                payer=opened.payer,
                submitter=opened.submitter,
                anchored_digest=opened.digest,
                total_minor=opened.total_minor,
                payee_count=opened.payee_count,
                pq_verified=opened.pq_verified,
                tx_hash=tx_hash,
                block_number=opened.block_number,
                payments=tuple(payments),
                header=header,
                token=token,
            )
        )

    runs.sort(key=lambda run: (run.block_number, run.tx_hash))
    return Ledger(runs=tuple(runs), from_block=from_block, to_block=resolved_to)
