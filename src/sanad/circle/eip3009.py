"""EIP-3009 authorizations for Arc USDC, so the payer signs and someone else submits.

Why this exists in a payout product: a corporate treasury will not put its signing key in
an application's process. The realistic shape is that the payer's funds sit in a custodied
wallet, the payer authorizes each payment off chain, and an operator submits the batch and
pays the gas. EIP-3009 is exactly that primitive, and Arc USDC is a Circle FiatToken so it
ships with it.

The pleasant part is that it composes with everything else here. `transferWithAuthorization`
takes the payer from the signature rather than from `msg.sender`, so the USDC `Transfer`
event still names the payer even though the operator sent the transaction. Wrap that call
in Arc's Memo and the instruction rides along. Batch it through Multicall3From and a whole
run settles in one transaction that the payer never had to broadcast.

Domain values are Arc USDC's own, confirmed against the live contract: name "USDC",
version "2", chainId 5042002, verifyingContract 0x3600000000000000000000000000000000000000.

The typed data layout and the Arc domain values were established in the sibling MoonWalk
build (`work/moonwalk/src/circle/wallets.py`), where Circle signed one of these and a
relayer submitted it successfully. Written fresh here for the payout case.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any, Final, Literal

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak
from web3 import Web3

from ..arc import addresses

TypedData = dict[str, Any]

AuthorizationKind = Literal["TransferWithAuthorization", "ReceiveWithAuthorization"]

EIP712_DOMAIN: Final[list[dict[str, str]]] = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

AUTHORIZATION_FIELDS: Final[list[dict[str, str]]] = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]

USDC_DOMAIN_NAME: Final[str] = "USDC"
USDC_DOMAIN_VERSION: Final[str] = "2"

TRANSFER_WITH_AUTHORIZATION_SELECTOR: Final[bytes] = keccak(
    text="transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
)[:4]


class AuthorizationError(ValueError):
    """An authorization that Arc USDC would reject."""


def new_nonce() -> bytes:
    """A random 32 byte nonce. EIP-3009 nonces are not sequential, they are single use
    values the token records, so randomness is the whole requirement."""
    return secrets.token_bytes(32)


def authorization_typed_data(
    *,
    payer: str,
    payee: str,
    value: int,
    nonce: bytes,
    valid_after: int = 0,
    valid_before: int | None = None,
    kind: AuthorizationKind = "TransferWithAuthorization",
    token: str = addresses.USDC,
    chain_id: int = addresses.CHAIN_ID,
) -> TypedData:
    """The payload the payer signs for one payout.

    `valid_before` defaults to one hour out. A payout run is signed and submitted in the
    same minute, so a short window limits how long a leaked authorization is worth
    anything, and an expired one simply reverts.
    """
    if value <= 0:
        raise AuthorizationError("value must be positive")
    if len(nonce) != 32:
        raise AuthorizationError(f"nonce must be 32 bytes, got {len(nonce)}")
    if valid_before is None:
        valid_before = int(dt.datetime.now(tz=dt.timezone.utc).timestamp()) + 3600
    if valid_before <= valid_after:
        raise AuthorizationError("validBefore must be after validAfter")

    return {
        "types": {"EIP712Domain": EIP712_DOMAIN, kind: AUTHORIZATION_FIELDS},
        "primaryType": kind,
        "domain": {
            "name": USDC_DOMAIN_NAME,
            "version": USDC_DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": Web3.to_checksum_address(token),
        },
        "message": {
            "from": Web3.to_checksum_address(payer),
            "to": Web3.to_checksum_address(payee),
            "value": value,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": "0x" + nonce.hex(),
        },
    }


def split_signature(signature: bytes) -> tuple[int, bytes, bytes]:
    """Split 65 bytes into v, r, s.

    Arc USDC is FiatToken v2, whose `transferWithAuthorization` takes v, r and s rather
    than a packed `bytes`. Signers return either 27/28 or 0/1 for v, so both are accepted
    and normalised, because getting this wrong looks identical to a bad signature.
    """
    if len(signature) != 65:
        raise AuthorizationError(f"expected a 65 byte signature, got {len(signature)}")
    r, s, v = signature[:32], signature[32:64], signature[64]
    if v in (0, 1):
        v += 27
    if v not in (27, 28):
        raise AuthorizationError(f"recovery id {v} is not 27 or 28")
    return v, r, s


def encode_transfer_with_authorization(typed_data: TypedData, signature: bytes) -> bytes:
    """Calldata for `USDC.transferWithAuthorization`, ready to be wrapped in a Memo."""
    message = typed_data["message"]
    v, r, s = split_signature(signature)
    nonce = message["nonce"]
    return TRANSFER_WITH_AUTHORIZATION_SELECTOR + abi_encode(
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
        [
            message["from"],
            message["to"],
            int(message["value"]),
            int(message["validAfter"]),
            int(message["validBefore"]),
            bytes.fromhex(nonce[2:]) if isinstance(nonce, str) else nonce,
            v,
            r,
            s,
        ],
    )
