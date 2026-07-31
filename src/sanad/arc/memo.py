"""Arc's Memo contract, as calldata in and events out.

`Memo.memo(target, data, memoId, memoData)` routes the inner call through the CallFrom
precompile, so `target` sees the payer's EOA as `msg.sender` and needs no knowledge of
memos, no approval and no custody wrapper. Everything here is pure: build bytes, parse
logs, no network. The sender lives in `sanad.chain`.

Two facts that shape this module, both learned from the node source rather than from
docs:

* CallFrom's allowlist is hardcoded to Memo and Multicall3From, so these two contracts
  are the only way to get sender preservation. Writing our own caller is not an option.
* Memo is EOA only. A contract calling it fails inside CallFrom rather than raising
  `MemoFailed`, so a revert with no `MemoFailed` selector usually means something in
  the path is a contract when it should not be.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from . import addresses

# memo(address,bytes,bytes32,bytes)
MEMO_SELECTOR: bytes = keccak(text="memo(address,bytes,bytes32,bytes)")[:4]
# transfer(address,uint256), the inner call for a payout
TRANSFER_SELECTOR: bytes = keccak(text="transfer(address,uint256)")[:4]


def encode_transfer(to: str, amount: int) -> bytes:
    """ERC-20 transfer calldata. The inner call of a payout, executed as the payer."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    return TRANSFER_SELECTOR + abi_encode(["address", "uint256"], [to, amount])


def encode_memo_call(target: str, data: bytes, memo_id: bytes, memo_data: bytes) -> bytes:
    """Calldata for `Memo.memo`. `memo_id` is the indexed topic, `memo_data` the payload."""
    if len(memo_id) != 32:
        raise ValueError(f"memo_id must be 32 bytes, got {len(memo_id)}")
    return MEMO_SELECTOR + abi_encode(
        ["address", "bytes", "bytes32", "bytes"], [target, data, memo_id, memo_data]
    )


def call_data_hash(data: bytes) -> bytes:
    """What Memo puts in `callDataHash`, so a memo can be tied to the exact call it
    describes. Recomputing this locally is how a verifier proves the pair was not
    reassembled after the fact."""
    return keccak(data)


@dataclass(frozen=True, slots=True)
class MemoLog:
    """A decoded `Memo` event."""

    sender: str
    target: str
    memo_id: bytes
    call_data_hash: bytes
    memo: bytes
    memo_index: int
    block_number: int
    tx_hash: str
    log_index: int

    def binds(self, data: bytes) -> bool:
        """True when this memo commits to exactly `data`."""
        return self.call_data_hash == call_data_hash(data)


def _topic_address(topic: Any) -> str:
    raw = topic.hex() if isinstance(topic, bytes) else str(topic)
    return "0x" + raw[-40:]


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    text = str(value)
    return bytes.fromhex(text[2:] if text.startswith("0x") else text)


def decode_memo_log(log: Any) -> MemoLog:
    """Parse a raw `eth_getLogs` entry from the Memo contract.

    Hand decoded rather than run through a contract object, because the indexer reads
    logs in bulk and building a web3 contract per log is wasteful. Layout:
    topics are (signature, sender, target, memoId) and the data words are
    callDataHash, then the offset to `memo`, then memoIndex.
    """
    topics = [_as_bytes(t) for t in (log["topics"] if isinstance(log, dict) else log.topics)]
    if len(topics) != 4:
        raise ValueError(f"expected 4 topics on a Memo event, got {len(topics)}")
    if "0x" + topics[0].hex() != addresses.TOPIC_MEMO:
        raise ValueError("log is not a Memo event")

    data = _as_bytes(log["data"] if isinstance(log, dict) else log.data)
    words = [data[i : i + 32] for i in range(0, len(data), 32)]
    offset = int.from_bytes(words[1], "big")
    length = int.from_bytes(data[offset : offset + 32], "big")

    def field(name: str) -> Any:
        return log[name] if isinstance(log, dict) else getattr(log, name)

    tx_hash = field("transactionHash")
    return MemoLog(
        sender=_topic_address(topics[1]),
        target=_topic_address(topics[2]),
        memo_id=topics[3],
        call_data_hash=words[0],
        memo=data[offset + 32 : offset + 32 + length],
        memo_index=int.from_bytes(words[2], "big"),
        block_number=int(field("blockNumber"), 16)
        if isinstance(field("blockNumber"), str)
        else int(field("blockNumber")),
        tx_hash=tx_hash if isinstance(tx_hash, str) else "0x" + _as_bytes(tx_hash).hex(),
        log_index=int(field("logIndex"), 16)
        if isinstance(field("logIndex"), str)
        else int(field("logIndex")),
    )


# transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)
TRANSFER_WITH_AUTHORIZATION_SELECTOR: bytes = keccak(
    text="transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
)[:4]


@dataclass(frozen=True, slots=True)
class MemoCall:
    """A `Memo.memo` call, decoded from calldata."""

    target: str
    data: bytes
    memo_id: bytes
    memo_data: bytes

    def binds_its_own_data(self, recorded_hash: bytes) -> bool:
        return call_data_hash(self.data) == recorded_hash


def decode_memo_call(calldata: bytes) -> MemoCall:
    """Unpack the four arguments of `Memo.memo`."""
    if calldata[:4] != MEMO_SELECTOR:
        raise ValueError(f"not a Memo.memo call, selector is 0x{calldata[:4].hex()}")
    target, data, memo_id, memo_data = abi_decode(
        ["address", "bytes", "bytes32", "bytes"], calldata[4:]
    )
    return MemoCall(
        target=str(target), data=bytes(data), memo_id=bytes(memo_id), memo_data=bytes(memo_data)
    )


@dataclass(frozen=True, slots=True)
class PaymentCall:
    """The payee and amount, read out of whichever transfer shape the memo wrapped.

    `payer` is set only for `transferWithAuthorization`, where the sender comes from the
    signature rather than from `msg.sender`. That is the whole reason an operator can
    submit a run without becoming the payer, so it is worth surfacing rather than
    flattening away.
    """

    payee: str
    amount_minor: int
    payer: str | None = None
    kind: str = "transfer"


def decode_payment_call(data: bytes) -> PaymentCall:
    """Recognise the two call shapes a Sanad payout can take."""
    selector = data[:4]
    if selector == TRANSFER_SELECTOR:
        payee, amount = abi_decode(["address", "uint256"], data[4:])
        return PaymentCall(payee=str(payee), amount_minor=int(amount), kind="transfer")
    if selector == TRANSFER_WITH_AUTHORIZATION_SELECTOR:
        payer, payee, amount = abi_decode(
            [
                "address",
                "address",
                "uint256",
                "uint256",
                "uint256",
                "bytes32",
                "uint8",
                "bytes32",
                "bytes32",
            ],
            data[4:],
        )[:3]
        return PaymentCall(
            payee=str(payee),
            amount_minor=int(amount),
            payer=str(payer),
            kind="transferWithAuthorization",
        )
    raise ValueError(f"unrecognised payment call, selector 0x{selector.hex()}")
