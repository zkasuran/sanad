"""Arc's own denylist, read directly.

Every compliance layer in this hackathon is going to be a vendor API call or a hardcoded
list. Arc ships the list on chain, because the chain itself enforces it: a denylisted
address cannot receive a value transfer, and the transfer reverts inside the token rather
than being flagged afterwards.

So screening a payout run is a read, not a subscription. On testnet the Denylist is a
CREATE2 address mined under the `0x360` system prefix; on mainnet it takes the next
system contract slot, which is empty on testnet by design. Both are in
`sanad.arc.addresses`.

This matters for a batch specifically. One denylisted payee in a run of forty would
revert the whole transaction if the batch were built with `allowFailure=False`, and would
silently skip that payee if built with `allowFailure=True`. Neither is what a payments
operator wants. They want to know before signing, so `screen` runs first and the run
either goes clean or the operator is told exactly which line to pull.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from . import addresses

IS_DENYLISTED_SELECTOR: bytes = keccak(text="isDenylisted(address)")[:4]
IS_DENYLISTER_SELECTOR: bytes = keccak(text="isDenylister(address)")[:4]


def encode_is_denylisted(address: str) -> bytes:
    return IS_DENYLISTED_SELECTOR + abi_encode(["address"], [address])


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """The answer for one run, in the shape a signing decision needs."""

    checked: tuple[str, ...]
    denylisted: tuple[str, ...]
    denylist_address: str

    @property
    def clean(self) -> bool:
        return not self.denylisted

    def explain(self) -> str:
        if self.clean:
            return (
                f"{len(self.checked)} payee(s) screened against Arc's own denylist at "
                f"{self.denylist_address}, none denylisted"
            )
        listed = ", ".join(self.denylisted)
        return (
            f"{len(self.denylisted)} of {len(self.checked)} payee(s) are denylisted on "
            f"Arc: {listed}. Their transfers would revert inside USDC, so pull those "
            "lines before signing"
        )


def screen(
    call: object,
    payees: Iterable[str],
    *,
    denylist: str = addresses.DENYLIST_TESTNET,
) -> ScreeningResult:
    """Check every payee. `call` is anything with a `call(to, data)` method, which in
    practice is `sanad.chain.ArcClient`, kept loose so this module needs no import of it
    and stays testable with a stub.

    Duplicates are collapsed, because a run can legitimately pay the same counterparty
    for two invoices and there is no reason to ask the chain twice.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for payee in payees:
        key = payee.lower()
        if key not in seen:
            seen.add(key)
            unique.append(payee)

    flagged: list[str] = []
    for payee in unique:
        raw = call.call(denylist, encode_is_denylisted(payee))  # type: ignore[attr-defined]
        if int.from_bytes(raw, "big") != 0:
            flagged.append(payee)

    return ScreeningResult(
        checked=tuple(unique), denylisted=tuple(flagged), denylist_address=denylist
    )
