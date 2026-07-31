"""Reaching Arc, and getting a transaction mined.

Small on purpose. The payment logic is pure and lives in `sanad.payouts`,
`sanad.arc.memo` and `sanad.arc.batch`, so this module only knows how to talk to a node
and how to sign.

Two Arc specifics are baked in because they cost time to discover:

* The public RPC rate limits a chatty client and answers `-32011 request limit reached`.
  Response caching and dropping web3's per call validation middleware cut the request
  count enough to stay under it.
* USDC is the gas token and shows up twice: as the native balance at 18 decimals and as
  an ERC-20 balance at 6. They are the same money, so `balance_minor` reads the ERC-20
  view and `native_balance` is only there for gas arithmetic.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from eth_abi.abi import decode as abi_decode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from web3 import Web3

from .arc import addresses

logger = logging.getLogger("sanad.chain")

BALANCE_OF_SELECTOR: bytes = keccak(text="balanceOf(address)")[:4]


@dataclass(frozen=True, slots=True)
class SentTx:
    """A mined transaction, reduced to what a receipt line or a piece of evidence needs."""

    tx_hash: str
    block_number: int
    gas_used: int
    status: int
    effective_gas_price: int
    logs: tuple[Any, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == 1

    @property
    def fee_minor(self) -> int:
        """Fee in USDC minor units. Gas is priced in native wei at 18 decimals and USDC
        is 6, so the factor between the two views has to come out here."""
        return (self.gas_used * self.effective_gas_price) // addresses.NATIVE_TO_ERC20_FACTOR

    @property
    def fee(self) -> float:
        return (self.gas_used * self.effective_gas_price) / 10**addresses.NATIVE_DECIMALS

    @property
    def url(self) -> str:
        return addresses.tx_url(self.tx_hash)


class ArcClient:
    """A signer plus a node. One instance per key."""

    def __init__(
        self,
        private_key: str | None = None,
        *,
        rpc_url: str = addresses.RPC_URL,
        expect_chain_id: int = addresses.CHAIN_ID,
    ) -> None:
        provider = Web3.HTTPProvider(rpc_url, cache_allowed_requests=True)
        self.w3 = Web3(provider)
        try:
            self.w3.middleware_onion.remove("validation")
        except (KeyError, ValueError):  # pragma: no cover, web3 version dependent
            logger.debug("no validation middleware to remove")
        # A key is optional on purpose. Rebuilding the ledger is a read, so the audit
        # path needs no signer and no database, and that is worth being able to show.
        self.account: LocalAccount | None = (
            Account.from_key(private_key) if private_key else None
        )
        chain_id = self.w3.eth.chain_id
        if chain_id != expect_chain_id:
            raise RuntimeError(f"connected to chain {chain_id}, expected {expect_chain_id}")
        self.chain_id = chain_id

    @property
    def address(self) -> str:
        if self.account is None:
            raise RuntimeError("this client is read only, it was built without a key")
        return self.account.address

    @property
    def can_sign(self) -> bool:
        return self.account is not None

    def call(self, to: str, data: bytes, *, sender: str | None = None) -> bytes:
        caller = sender or (self.address if self.can_sign else None)
        payload: dict[str, object] = {"to": to, "data": data}
        if caller:
            payload["from"] = caller
        return bytes(self.w3.eth.call(payload))  # type: ignore[arg-type]

    def estimate(self, to: str, data: bytes) -> int:
        return int(self.w3.eth.estimate_gas({"to": to, "data": data, "from": self.address}))  # type: ignore[arg-type]

    def balance_minor(self, address: str | None = None, token: str = addresses.USDC) -> int:
        """ERC-20 balance in minor units, the number a payout run is measured in."""
        target = address or self.address
        raw = self.call(token, BALANCE_OF_SELECTOR + bytes(12) + bytes.fromhex(target[2:]))
        return int.from_bytes(raw, "big")

    def native_balance(self, address: str | None = None) -> int:
        return int(self.w3.eth.get_balance(address or self.address))  # type: ignore[arg-type]

    def send(self, to: str, data: bytes, *, gas: int | None = None, gas_buffer: float = 1.25) -> SentTx:
        """Sign, send and wait. Estimates gas unless told otherwise, with a buffer,
        because a batch that runs out of gas costs the fee and pays nobody."""
        if self.account is None:
            raise RuntimeError("cannot send from a read only client")
        if gas is None:
            gas = int(self.estimate(to, data) * gas_buffer)
        tx = {
            "to": to,
            "data": data,
            "gas": gas,
            "nonce": self.w3.eth.get_transaction_count(self.address),  # type: ignore[arg-type]
            "chainId": self.chain_id,
            "maxFeePerGas": self.w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": self.w3.to_wei(5, "gwei"),
        }
        signed = self.account.sign_transaction(tx)  # type: ignore[arg-type]
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        raw_hash = receipt["transactionHash"]
        hex_hash = raw_hash.hex() if isinstance(raw_hash, bytes) else str(raw_hash)
        sent = SentTx(
            tx_hash=hex_hash if hex_hash.startswith("0x") else "0x" + hex_hash,
            block_number=int(receipt["blockNumber"]),
            gas_used=int(receipt["gasUsed"]),
            status=int(receipt["status"]),
            effective_gas_price=int(receipt.get("effectiveGasPrice", 0)),
            logs=tuple(receipt["logs"]),
        )
        logger.info("sent %s gas=%s fee=%.6f USDC", sent.tx_hash, sent.gas_used, sent.fee)
        return sent

    def logs(
        self,
        *,
        address: str,
        topics: list[Any] | None = None,
        from_block: int | str = 0,
        to_block: int | str = "latest",
        window: int = 20_000,
    ) -> list[Any]:
        """Read logs in block windows, honouring the range the RPC asks for.

        The public Arc testnet RPC caps a single `eth_getLogs` at 20,000 results and
        refuses anything wider, either with `413 Payload Too Large` or with
        `-32602 query exceeds max results 20000, retry with the range A-B`. It names the
        range it will accept, so that hint is used when present and the window is
        quartered when it is not. The audit therefore works from a laptop against the
        public endpoint with no paid archive node.
        """
        start = int(from_block) if not isinstance(from_block, str) or from_block.isdigit() else 0
        end = int(self.w3.eth.block_number) if to_block == "latest" else int(to_block)
        span, floor = max(1, window), 1
        throttled = 0
        found: list[Any] = []
        while start <= end:
            stop = min(start + span - 1, end)
            try:
                found.extend(
                    self.w3.eth.get_logs(
                        {  # type: ignore[arg-type]
                            "address": address,
                            "topics": topics or [],
                            "fromBlock": start,
                            "toBlock": stop,
                        }
                    )
                )
            except Exception as exc:  # noqa: BLE001 - the RPC signals overflow many ways
                text = str(exc).lower()
                if "429" in text or "too many requests" in text:
                    if throttled >= 5:
                        raise
                    throttled += 1
                    pause = 2 ** throttled
                    logger.info("rpc rate limited, waiting %ss before retrying %s..%s", pause, start, stop)
                    time.sleep(pause)
                    continue
                overflow = any(
                    marker in text
                    for marker in (
                        "payload too large",
                        "413",
                        "max results",
                        "max allowed range",
                        "too many",
                        "more than",
                    )
                )
                hint = re.search(r"retry with the range (\d+)\s*-\s*(\d+)", text)
                if hint and int(hint.group(2)) >= start:
                    span = max(floor, int(hint.group(2)) - start + 1)
                    logger.info("rpc asked for %s..%s, window now %s blocks", start, hint.group(2), span)
                    continue
                if not overflow or span <= floor:
                    raise
                span = max(floor, span // 4)
                logger.info("log window too wide at %s..%s, retrying at %s blocks", start, stop, span)
                continue
            start = stop + 1
        return found

    def decode_single(self, types: list[str], data: bytes) -> Any:
        return abi_decode(types, data)
