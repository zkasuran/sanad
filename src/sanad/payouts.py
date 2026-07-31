"""A payout run: the unit the payer authorizes, and the calls it turns into.

One run is one mandate and one or more transactions. Each payee becomes a `Memo` call
wrapping a `transfer`, so the money and its ISO 20022 instruction land together, and
the whole set goes through Multicall3From so every transfer still attributes to the
payer's own address.

The run is the thing that gets signed. Its digest covers the payer, the token, and
every payee with its amount and its memo id in order, so a batch cannot be reordered,
padded or repriced after authorization without the signature failing. Signing lives in
`sanad.mandate`, because a run needs to be describable without a key present.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from . import iso20022
from .arc import addresses
from .arc.batch import Call3
from .arc.memo import encode_memo_call, encode_transfer
from .iso20022 import PaymentInstruction


class PayoutError(ValueError):
    """A run that cannot be authorized as described."""


@dataclass(frozen=True, slots=True)
class Payee:
    """One line of a run. The instruction is what makes it explainable later."""

    address: str
    amount_minor: int
    instruction: PaymentInstruction

    def __post_init__(self) -> None:
        if not (self.address.startswith("0x") and len(self.address) == 42):
            raise PayoutError(f"{self.address!r} is not an address")
        if self.amount_minor <= 0:
            raise PayoutError(f"{self.instruction.end_to_end_id}: amount must be positive")

    @property
    def memo_id(self) -> bytes:
        return iso20022.memo_id(self.instruction.end_to_end_id)

    @property
    def memo_bytes(self) -> bytes:
        return iso20022.encode(self.instruction)

    @property
    def amount(self) -> float:
        return self.amount_minor / 10**addresses.USDC_DECIMALS


@dataclass(frozen=True, slots=True)
class PayoutRun:
    """A named set of payouts, authorized as one thing."""

    run_id: str
    payer: str
    payees: tuple[Payee, ...]
    token: str = addresses.USDC
    created_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(tz=dt.timezone.utc)
    )

    def __post_init__(self) -> None:
        if not self.payees:
            raise PayoutError("a run needs at least one payee")
        if not self.run_id:
            raise PayoutError("run_id is required")
        seen: set[str] = set()
        for payee in self.payees:
            reference = payee.instruction.end_to_end_id
            if reference in seen:
                raise PayoutError(
                    f"end to end id {reference!r} appears twice. It is the payment's "
                    "identity and the memo id topic, so it has to be unique in a run"
                )
            seen.add(reference)

    @property
    def total_minor(self) -> int:
        return sum(payee.amount_minor for payee in self.payees)

    @property
    def total(self) -> float:
        return self.total_minor / 10**addresses.USDC_DECIMALS

    @property
    def memo_byte_count(self) -> int:
        """How many bytes of instruction this run writes to the chain."""
        return sum(len(payee.memo_bytes) for payee in self.payees)

    def build_calls(self, *, allow_failure: bool = True) -> list[Call3]:
        """Turn the run into the batch entries.

        `allow_failure` defaults to true: in a payout run of forty suppliers, one
        denylisted or malformed payee should be reported rather than reverting the
        thirty nine that were fine. The caller reads the per entry results and retries
        the failures.
        """
        calls: list[Call3] = []
        for payee in self.payees:
            inner = encode_transfer(payee.address, payee.amount_minor)
            calls.append(
                Call3(
                    target=addresses.MEMO,
                    call_data=encode_memo_call(
                        target=self.token,
                        data=inner,
                        memo_id=payee.memo_id,
                        memo_data=payee.memo_bytes,
                    ),
                    allow_failure=allow_failure,
                )
            )
        return calls

    def mandate_digest(self) -> bytes:
        """What the payer authorizes.

        Order sensitive on purpose. Reordering a batch changes which payee is paid
        first out of a limited balance, so order is part of the authorization.

        Every input is recoverable from chain data: the run id enters as its keccak
        hash, which is also the indexed memo id of the run header, and the payees,
        amounts and memo ids all appear in the Memo and Transfer events of the
        settling transaction. That is what lets `sanad.ledger` recompute this digest
        during a rebuild and check it against the one the mandate anchored, with no
        database and no trust in the operator.
        """
        return mandate_digest_from_parts(
            run_id_hash=run_id_hash(self.run_id),
            payer=self.payer,
            token=self.token,
            chain_id=addresses.CHAIN_ID,
            payees=[payee.address for payee in self.payees],
            amounts=[payee.amount_minor for payee in self.payees],
            memo_ids=[payee.memo_id for payee in self.payees],
        )


def run_id_hash(run_id: str) -> bytes:
    """The indexed key for a run. Same shape as a memo id, different namespace."""
    return keccak(text=run_id)


def mandate_digest_from_parts(
    *,
    run_id_hash: bytes,
    payer: str,
    token: str,
    chain_id: int,
    payees: Sequence[str],
    amounts: Sequence[int],
    memo_ids: Sequence[bytes],
) -> bytes:
    """The digest, from pieces rather than from a run object.

    The ledger rebuild has payees, amounts and memo ids read off the chain and no
    `PayoutRun` to hand, so the hashing rule lives here and both callers use it. One
    definition, so a rebuild cannot silently disagree with a settlement.
    """
    if not (len(payees) == len(amounts) == len(memo_ids)):
        raise PayoutError("payees, amounts and memo ids must be the same length")
    return keccak(
        abi_encode(
            ["bytes32", "address", "address", "uint256", "address[]", "uint256[]", "bytes32[]"],
            [
                run_id_hash,
                payer,
                token,
                chain_id,
                list(payees),
                list(amounts),
                list(memo_ids),
            ],
        )
    )
