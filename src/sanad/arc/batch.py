"""Multicall3From: batching that keeps the payer as `msg.sender`.

Plain Multicall3 at `0xcA11bde0...` makes the callee see the multicall contract, which
breaks any accounting, allowance or authorization keyed on `msg.sender`. Arc's
Multicall3From routes every inner call through the CallFrom precompile instead, so N
payouts settle in one transaction and each USDC `Transfer` still reads as coming from
the payer's own address.

The composition that matters here is nesting it with Memo. Both contracts document
themselves as EOA only and read as uncomposable, because CallFrom insists the sender
argument equals either the precompile caller or `tx.origin`. Nest them and the EOA
survives both hops: Multicall3From passes `sender = tx.origin`, so Memo sees the EOA as
its own `msg.sender`, and Memo's own CallFrom call then passes the same test. The
result is a batch where every payment carries its own instruction. Proven on Arc
testnet in tx 0x15c1f3079e2d1be3393be7518223144f0d93c089d56fbba73a3c759f2aa077d2:
three payees, three memos, three purpose codes, 140,248 gas.

There is no `aggregate3Value`, because CallFrom does not forward value. That is fine
for USDC, which moves as an ERC-20 call and not as value.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

# aggregate3((address,bool,bytes)[]) keeps the standard Multicall3 signature, so the
# selector is the familiar one and any Multicall3 tooling can read the calldata.
AGGREGATE3_SELECTOR: bytes = keccak(text="aggregate3((address,bool,bytes)[])")[:4]
AGGREGATE3_ARG_TYPE: str = "(address,bool,bytes)[]"
AGGREGATE3_RETURN_TYPE: str = "(bool,bytes)[]"


@dataclass(frozen=True, slots=True)
class Call3:
    """One entry in a batch. `allow_failure` decides whether a single bad payee stops
    the whole run or is reported and skipped."""

    target: str
    call_data: bytes
    allow_failure: bool = False


@dataclass(frozen=True, slots=True)
class Call3Result:
    """What the batch says happened to one entry."""

    success: bool
    return_data: bytes


def encode_aggregate3(calls: list[Call3]) -> bytes:
    """Calldata for `Multicall3From.aggregate3`."""
    if not calls:
        raise ValueError("a batch needs at least one call")
    tuples = [(call.target, call.allow_failure, call.call_data) for call in calls]
    return AGGREGATE3_SELECTOR + abi_encode([AGGREGATE3_ARG_TYPE], [tuples])


def decode_aggregate3_result(return_data: bytes) -> list[Call3Result]:
    """Parse the `Result[]` that `aggregate3` returns."""
    (rows,) = abi_decode([AGGREGATE3_RETURN_TYPE], return_data)
    return [Call3Result(success=bool(success), return_data=bytes(data)) for success, data in rows]


def split_into_batches(calls: list[Call3], *, max_per_batch: int) -> list[list[Call3]]:
    """Chop a payout run into transactions.

    Arc's block gas limit is 30,000,000 and a memo wrapped transfer costs roughly
    45,000 gas inside a batch, so the ceiling is in the hundreds rather than the tens.
    The real limit in practice is the RPC and the wallet, so the batch size stays a
    caller decision and this helper only enforces it.
    """
    if max_per_batch < 1:
        raise ValueError("max_per_batch must be at least 1")
    return [calls[i : i + max_per_batch] for i in range(0, len(calls), max_per_batch)]


def decode_aggregate3_calldata(calldata: bytes) -> list[Call3]:
    """Read a submitted batch back out of the transaction that carried it.

    The rebuild needs this because a memo commits to the hash of its inner call, and
    verifying that commitment means having the call itself. Events do not carry it, but
    the transaction input does, and transaction input is chain data like any other. One
    `eth_getTransactionByHash` per run is the whole cost.
    """
    if calldata[:4] != AGGREGATE3_SELECTOR:
        raise ValueError(
            f"not an aggregate3 call, selector is 0x{calldata[:4].hex()} "
            f"and aggregate3 is 0x{AGGREGATE3_SELECTOR.hex()}"
        )
    (rows,) = abi_decode([AGGREGATE3_ARG_TYPE], calldata[4:])
    return [
        Call3(target=str(target), call_data=bytes(data), allow_failure=bool(allow_failure))
        for target, allow_failure, data in rows
    ]
