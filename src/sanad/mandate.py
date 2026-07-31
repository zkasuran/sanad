"""The mandate: what the payer authorizes, anchored in the transaction that pays it.

`SanadMandate.open` is called through Arc's Memo contract, so `msg.sender` inside it is
the payer's own EOA. That has a pleasant consequence: the transaction itself is the
classical authorization, and there is no ECDSA signature to pass around or replay. What
the mandate adds on top is an optional post quantum signature over the run digest,
verified on chain by Arc's PQ precompile.

The mandate call is also carried by a Memo, so a run announces itself with an indexed
`memoId` of `keccak(run_id)`. That makes "show me run RUN-2026-07-31-A" one
`eth_getLogs` filter, and it puts a compact run header on chain next to the mandate.

Run header wire format, version 1, 47 bytes:

```
offset  size  field
0       4     magic, ASCII "SNDR"
4       1     version, 0x01
5       2     payee count, uint16
7       8     total in token minor units, uint64
15      32    mandate digest
```
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from .arc import addresses
from .iso20022 import CodecError
from .payouts import PayoutRun, run_id_hash

OPEN_SELECTOR: Final[bytes] = keccak(
    text="open(bytes32,bytes32,uint256,uint32,address,bytes,bytes)"
)[:4]
TOPIC_MANDATE_OPENED: Final[str] = (
    "0x"
    + keccak(text="MandateOpened(bytes32,address,address,bytes32,uint256,uint32,bool)").hex()
)

RUN_HEADER_MAGIC: Final[bytes] = b"SNDR"
RUN_HEADER_VERSION: Final[int] = 0x02
RUN_HEADER_V1_LEN: Final[int] = 47
RUN_HEADER_FIXED_LEN: Final[int] = 47
RUN_ID_MAX: Final[int] = 40

_DEPLOYMENTS = Path(__file__).resolve().parents[2] / "deployments" / "arc-testnet.json"


def mandate_address() -> str:
    """Where SanadMandate lives on Arc testnet, read from the committed deployment
    record rather than hardcoded, so a redeploy is a one line change."""
    with _DEPLOYMENTS.open() as handle:
        record: dict[str, Any] = json.load(handle)
    return str(record["contracts"]["SanadMandate"]["address"])


__all__ = [
    "MandateOpened",
    "RunHeader",
    "decode_mandate_opened",
    "decode_run_header",
    "encode_open",
    "encode_run_header",
    "mandate_address",
    "run_id_hash",
]


def encode_open(
    run_id: str,
    digest: bytes,
    total_minor: int,
    payee_count: int,
    *,
    payer: str | None = None,
    pq_vk: bytes = b"",
    pq_sig: bytes = b"",
) -> bytes:
    """Calldata for `SanadMandate.open`."""
    if len(digest) != 32:
        raise CodecError("digest must be 32 bytes")
    if payee_count <= 0 or total_minor <= 0:
        raise CodecError("a run with no payees or no value cannot be authorized")
    if (pq_vk or pq_sig) and (
        len(pq_vk) != addresses.PQ_VERIFYING_KEY_LEN or len(pq_sig) != addresses.PQ_SIGNATURE_LEN
    ):
        raise CodecError(
            f"SLH-DSA needs a {addresses.PQ_VERIFYING_KEY_LEN} byte key and a "
            f"{addresses.PQ_SIGNATURE_LEN} byte signature, got {len(pq_vk)} and {len(pq_sig)}"
        )
    return OPEN_SELECTOR + abi_encode(
        ["bytes32", "bytes32", "uint256", "uint32", "address", "bytes", "bytes"],
        [
            run_id_hash(run_id),
            digest,
            total_minor,
            payee_count,
            payer or "0x" + "0" * 40,
            pq_vk,
            pq_sig,
        ],
    )


def encode_run_header(run: PayoutRun) -> bytes:
    """The memo payload that rides along with the mandate call.

    Version 2 appends the run id in the clear. The digest already commits to its hash,
    so this adds nothing to the security argument, but it means a reconciler reading the
    chain sees `RUN-20260731-020957` rather than a bare 32 bytes, and the rebuild can
    label a run without asking anyone what it was called.
    """
    if len(run.payees) > 0xFFFF:
        raise CodecError("more than 65,535 payees in one run is not a run, it is a migration")
    run_id = run.run_id.encode("ascii", errors="strict")
    if len(run_id) > RUN_ID_MAX:
        raise CodecError(f"run_id is {len(run_id)} bytes, this format allows {RUN_ID_MAX}")
    return (
        RUN_HEADER_MAGIC
        + bytes([RUN_HEADER_VERSION])
        + len(run.payees).to_bytes(2, "big")
        + run.total_minor.to_bytes(8, "big")
        + run.mandate_digest()
        + bytes([len(run_id)])
        + run_id
    )


@dataclass(frozen=True, slots=True)
class RunHeader:
    payee_count: int
    total_minor: int
    digest: bytes
    version: int = RUN_HEADER_VERSION
    run_id: str | None = None


def decode_run_header(data: bytes) -> RunHeader:
    """Read a run header. Version 1 headers still decode, they just carry no run id."""
    if len(data) < RUN_HEADER_FIXED_LEN:
        raise CodecError(f"a run header is at least {RUN_HEADER_FIXED_LEN} bytes, got {len(data)}")
    if data[:4] != RUN_HEADER_MAGIC:
        raise CodecError("not a Sanad run header")
    version = data[4]
    if version not in (0x01, 0x02):
        raise CodecError(f"unsupported run header version {version}")

    run_id: str | None = None
    if version == 0x01:
        if len(data) != RUN_HEADER_V1_LEN:
            raise CodecError(f"a version 1 run header is {RUN_HEADER_V1_LEN} bytes, got {len(data)}")
    else:
        length = data[47]
        if len(data) != RUN_HEADER_FIXED_LEN + 1 + length:
            raise CodecError("run header length does not match its run id length")
        run_id = data[48 : 48 + length].decode("ascii")

    return RunHeader(
        payee_count=int.from_bytes(data[5:7], "big"),
        total_minor=int.from_bytes(data[7:15], "big"),
        digest=data[15:47],
        version=version,
        run_id=run_id,
    )


@dataclass(frozen=True, slots=True)
class MandateOpened:
    """A decoded `MandateOpened` event."""

    run_id_hash: bytes
    payer: str
    submitter: str
    digest: bytes
    total_minor: int
    payee_count: int
    pq_verified: bool
    block_number: int
    tx_hash: str


def decode_mandate_opened(log: Any) -> MandateOpened:
    def as_bytes(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        text = str(value)
        return bytes.fromhex(text[2:] if text.startswith("0x") else text)

    topics = [as_bytes(t) for t in (log["topics"] if isinstance(log, dict) else log.topics)]
    if "0x" + topics[0].hex() != TOPIC_MANDATE_OPENED:
        raise CodecError("log is not a MandateOpened event")
    data = as_bytes(log["data"] if isinstance(log, dict) else log.data)
    words = [data[i : i + 32] for i in range(0, len(data), 32)]

    def field(name: str) -> Any:
        return log[name] if isinstance(log, dict) else getattr(log, name)

    tx_hash = field("transactionHash")
    return MandateOpened(
        run_id_hash=topics[1],
        payer="0x" + topics[2].hex()[-40:],
        submitter="0x" + topics[3].hex()[-40:],
        digest=words[0],
        total_minor=int.from_bytes(words[1], "big"),
        payee_count=int.from_bytes(words[2], "big"),
        pq_verified=bool(int.from_bytes(words[3], "big")),
        block_number=int(field("blockNumber")),
        tx_hash=tx_hash if isinstance(tx_hash, str) else "0x" + as_bytes(tx_hash).hex(),
    )
