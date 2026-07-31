"""The on chain payment instruction, and its wire format.

A cross border payment fails reconciliation when the money and the instruction
travel separately. SWIFT carries remittance information in the message, ISO 20022
carries it in structured fields, and a plain USDC transfer carries nothing at all,
so every stablecoin rail today keeps that context in a private database. Arc's Memo
contract closes that gap, and this module is the format that goes into it.

Design constraints, in the order they mattered:

1. **Cheap.** Memo bytes are calldata and event data, so every byte is paid for on
   every payment. The format is packed, big endian and has no padding. A supplier
   payment with a structured invoice reference fits in about 60 bytes.
2. **Recognisable in a block explorer.** The first eight bytes read as ASCII, so a
   human looking at raw hex on Arcscan sees `SNAD` followed by the ISO 20022 purpose
   code, then the end to end id in the clear. Opaque blobs are a bad look on a chain
   whose whole point is auditability.
3. **Faithful to ISO 20022.** Field names, the four character ExternalPurpose1Code
   and the Max35Text length limits are the real ones, so a payment operations person
   recognises what they are reading and a full pain.001 or pacs.008 document can be
   reconstructed from it.
4. **Extensible without a version war.** Optional sections are flagged, not
   positional, and an unknown flag is a decode error rather than a silent misread.

The full XML document, where one exists, stays off chain and is committed by hash.
That keeps the on chain footprint small while still making tampering detectable.

Wire format, version 1:

```
offset  size  field
0       4     magic, ASCII "SNAD"
4       1     version, 0x01
5       1     flags bitfield
6       4     purpose code, 4 ASCII, ISO 20022 ExternalPurpose1Code
10      1     length of end to end id, 1 to 35
11      n     end to end id, ASCII
+0      1     length of creditor reference, 0 to 35
+1      n     creditor reference, ASCII
   if FLAG_UAE_POP:
        3     CBUAE purpose of payment code, 3 ASCII, UAEFTS
   if FLAG_STRUCTURED:
        4     invoice date, days since 1970-01-01, uint32
        8     invoice amount in minor units, uint64
        1     length of invoice number, 1 to 35
        n     invoice number, ASCII
   if FLAG_DOCUMENT_HASH:
        32    sha256 of the full ISO 20022 document
   if FLAG_FX:
        3     source currency, ISO 4217 alpha-3
        8     source amount in minor units, uint64
        8     rate applied, scaled by 1e12, uint64
```
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Final

from . import uaefts

MAGIC: Final[bytes] = b"SNAD"
VERSION: Final[int] = 0x01

FLAG_STRUCTURED: Final[int] = 0b0000_0001
FLAG_DOCUMENT_HASH: Final[int] = 0b0000_0010
FLAG_FX: Final[int] = 0b0000_0100
FLAG_UAE_POP: Final[int] = 0b0000_1000
KNOWN_FLAGS: Final[int] = FLAG_STRUCTURED | FLAG_DOCUMENT_HASH | FLAG_FX | FLAG_UAE_POP

MAX35: Final[int] = 35
UINT32_MAX: Final[int] = 2**32 - 1
UINT64_MAX: Final[int] = 2**64 - 1
RATE_SCALE: Final[int] = 10**12
EPOCH: Final[dt.date] = dt.date(1970, 1, 1)

# A working subset of ISO 20022 ExternalPurpose1Code. These are the codes a UAE
# outbound payment run actually uses, and the list is deliberately closed so a typo
# fails at build time rather than landing on chain.
PURPOSE_CODES: Final[dict[str, str]] = {
    "SALA": "Salary payment",
    "SUPP": "Supplier payment",
    "TRAD": "Trade settlement",
    "GDDS": "Purchase and sale of goods",
    "SCVE": "Purchase and sale of services",
    "INTC": "Intra company payment",
    "COMM": "Commission",
    "DIVI": "Dividend",
    "TAXS": "Tax payment",
    "RENT": "Rent",
    "LOAN": "Loan",
    "PENS": "Pension payment",
    "FREX": "Foreign exchange",
    "CHAR": "Charity payment",
}


class CodecError(ValueError):
    """A payment instruction that cannot be represented, or bytes that are not one."""


def _ascii35(value: str, field: str, *, allow_empty: bool = False) -> bytes:
    raw = value.encode("ascii", errors="strict") if value else b""
    if not raw and not allow_empty:
        raise CodecError(f"{field} is required")
    if len(raw) > MAX35:
        raise CodecError(f"{field} is {len(raw)} bytes, ISO 20022 Max35Text allows {MAX35}")
    return raw


@dataclass(frozen=True, slots=True)
class FxLeg:
    """What the payer sent before conversion, so an AED funded payout can still show
    the sending currency and the rate that was applied."""

    source_currency: str
    source_amount_minor: int
    rate_scaled_1e12: int

    def __post_init__(self) -> None:
        if len(self.source_currency) != 3 or not self.source_currency.isalpha():
            raise CodecError("source_currency must be an ISO 4217 alpha-3 code")
        for name, value in (
            ("source_amount_minor", self.source_amount_minor),
            ("rate_scaled_1e12", self.rate_scaled_1e12),
        ):
            if not 0 <= value <= UINT64_MAX:
                raise CodecError(f"{name} out of uint64 range")

    @property
    def rate(self) -> float:
        return self.rate_scaled_1e12 / RATE_SCALE


@dataclass(frozen=True, slots=True)
class StructuredRemittance:
    """ISO 20022 structured remittance information, reduced to the three fields that
    let a creditor match a payment to an invoice without a phone call."""

    invoice_number: str
    invoice_date: dt.date
    invoice_amount_minor: int

    def __post_init__(self) -> None:
        _ascii35(self.invoice_number, "invoice_number")
        days = (self.invoice_date - EPOCH).days
        if not 0 <= days <= UINT32_MAX:
            raise CodecError("invoice_date is outside the representable range")
        if not 0 <= self.invoice_amount_minor <= UINT64_MAX:
            raise CodecError("invoice_amount_minor out of uint64 range")


@dataclass(frozen=True, slots=True)
class PaymentInstruction:
    """One payment, and everything a reconciler or a regulator needs to explain it.

    `end_to_end_id` is the ISO 20022 EndToEndId, the identifier that survives the
    whole payment chain. It is also what `memo_id` hashes, so an invoice reference
    becomes an indexed topic and a lookup is one `eth_getLogs` call.
    """

    end_to_end_id: str
    purpose: str
    uae_purpose: str = ""
    creditor_reference: str = ""
    remittance: StructuredRemittance | None = None
    document_sha256: bytes | None = None
    fx: FxLeg | None = None

    def __post_init__(self) -> None:
        _ascii35(self.end_to_end_id, "end_to_end_id")
        _ascii35(self.creditor_reference, "creditor_reference", allow_empty=True)
        if self.purpose not in PURPOSE_CODES:
            known = ", ".join(sorted(PURPOSE_CODES))
            raise CodecError(f"purpose {self.purpose!r} is not a supported code. Known: {known}")
        if self.uae_purpose:
            # The domestic code is the Central Bank's table, not ISO 20022's, so it is
            # validated against the real UAEFTS list and a withdrawn code is named.
            try:
                uaefts.validate(self.uae_purpose)
            except uaefts.UnknownPurposeCode as exc:
                raise CodecError(str(exc)) from exc
        if self.document_sha256 is not None and len(self.document_sha256) != 32:
            raise CodecError("document_sha256 must be 32 bytes")

    @property
    def purpose_description(self) -> str:
        return PURPOSE_CODES[self.purpose]

    @property
    def flags(self) -> int:
        flags = 0
        if self.remittance is not None:
            flags |= FLAG_STRUCTURED
        if self.document_sha256 is not None:
            flags |= FLAG_DOCUMENT_HASH
        if self.fx is not None:
            flags |= FLAG_FX
        if self.uae_purpose:
            flags |= FLAG_UAE_POP
        return flags

    @property
    def uae_purpose_description(self) -> str:
        return uaefts.describe(self.uae_purpose) if self.uae_purpose else ""


class _Reader:
    """A bounds checked cursor. Every read names the field it is reading, so a
    malformed memo produces a message a human can act on."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    def take(self, n: int, field: str) -> bytes:
        if self._at + n > len(self._data):
            raise CodecError(
                f"truncated at {field}: wanted {n} bytes at offset {self._at}, "
                f"have {len(self._data) - self._at}"
            )
        chunk = self._data[self._at : self._at + n]
        self._at += n
        return chunk

    def take_u8(self, field: str) -> int:
        return self.take(1, field)[0]

    def take_uint(self, n: int, field: str) -> int:
        return int.from_bytes(self.take(n, field), "big")

    def take_ascii35(self, field: str) -> str:
        return self.take(self.take_u8(f"{field} length"), field).decode("ascii")

    @property
    def remaining(self) -> int:
        return len(self._data) - self._at


def encode(instruction: PaymentInstruction) -> bytes:
    """Pack an instruction into the bytes that go into `Memo.memo`."""
    out = bytearray(MAGIC)
    out.append(VERSION)
    out.append(instruction.flags)
    out += instruction.purpose.encode("ascii")

    end_to_end = _ascii35(instruction.end_to_end_id, "end_to_end_id")
    out.append(len(end_to_end))
    out += end_to_end

    creditor = _ascii35(instruction.creditor_reference, "creditor_reference", allow_empty=True)
    out.append(len(creditor))
    out += creditor

    if instruction.uae_purpose:
        out += instruction.uae_purpose.encode("ascii")

    if (remittance := instruction.remittance) is not None:
        out += (remittance.invoice_date - EPOCH).days.to_bytes(4, "big")
        out += remittance.invoice_amount_minor.to_bytes(8, "big")
        number = _ascii35(remittance.invoice_number, "invoice_number")
        out.append(len(number))
        out += number

    if instruction.document_sha256 is not None:
        out += instruction.document_sha256

    if (fx := instruction.fx) is not None:
        out += fx.source_currency.upper().encode("ascii")
        out += fx.source_amount_minor.to_bytes(8, "big")
        out += fx.rate_scaled_1e12.to_bytes(8, "big")

    return bytes(out)


def decode(data: bytes) -> PaymentInstruction:
    """Unpack memo bytes. Raises `CodecError` on anything this format cannot explain,
    including trailing bytes, because silently ignoring them is how two nodes end up
    disagreeing about what a payment said."""
    reader = _Reader(data)
    if reader.take(4, "magic") != MAGIC:
        raise CodecError("not a Sanad memo, magic bytes do not match")
    version = reader.take_u8("version")
    if version != VERSION:
        raise CodecError(f"unsupported version {version}, this build writes {VERSION}")
    flags = reader.take_u8("flags")
    if flags & ~KNOWN_FLAGS:
        raise CodecError(f"unknown flags 0x{flags & ~KNOWN_FLAGS:02x}, refusing to guess")

    purpose = reader.take(4, "purpose").decode("ascii")
    end_to_end_id = reader.take_ascii35("end_to_end_id")
    creditor_reference = reader.take_ascii35("creditor_reference")

    uae_purpose = ""
    if flags & FLAG_UAE_POP:
        uae_purpose = reader.take(3, "uae_purpose").decode("ascii")

    remittance: StructuredRemittance | None = None
    if flags & FLAG_STRUCTURED:
        days = reader.take_uint(4, "invoice_date")
        amount = reader.take_uint(8, "invoice_amount")
        remittance = StructuredRemittance(
            invoice_number=reader.take_ascii35("invoice_number"),
            invoice_date=EPOCH + dt.timedelta(days=days),
            invoice_amount_minor=amount,
        )

    document_sha256 = reader.take(32, "document_sha256") if flags & FLAG_DOCUMENT_HASH else None

    fx: FxLeg | None = None
    if flags & FLAG_FX:
        fx = FxLeg(
            source_currency=reader.take(3, "source_currency").decode("ascii"),
            source_amount_minor=reader.take_uint(8, "source_amount"),
            rate_scaled_1e12=reader.take_uint(8, "rate"),
        )

    if reader.remaining:
        raise CodecError(f"{reader.remaining} trailing bytes after a complete instruction")

    return PaymentInstruction(
        end_to_end_id=end_to_end_id,
        purpose=purpose,
        uae_purpose=uae_purpose,
        creditor_reference=creditor_reference,
        remittance=remittance,
        document_sha256=document_sha256,
        fx=fx,
    )


def memo_id(end_to_end_id: str) -> bytes:
    """The value that goes into Memo's indexed `memoId` topic.

    Hashing the ISO 20022 EndToEndId means an invoice reference becomes a searchable
    topic, so "show me the payment for INV-AE-1001" is one `eth_getLogs` filter rather
    than a scan. It is a commitment, not a secret: anyone holding the reference can
    recompute it, and anyone who does not cannot enumerate references from the chain.
    """
    from eth_utils.crypto import keccak

    return keccak(text=end_to_end_id)


def document_hash(document: bytes) -> bytes:
    """sha256 of a full ISO 20022 pain.001 or pacs.008 document.

    sha256 and not keccak, because the document is an artefact of the payments world
    and auditors there will reach for `sha256sum`, not a chain tool.
    """
    return hashlib.sha256(document).digest()


def describe(instruction: PaymentInstruction) -> str:
    """One line a human can read in a log or a receipt."""
    parts = [
        f"{instruction.end_to_end_id}",
        f"{instruction.purpose} ({instruction.purpose_description})",
    ]
    if instruction.uae_purpose:
        parts.append(f"CBUAE {instruction.uae_purpose} ({instruction.uae_purpose_description})")
    if instruction.creditor_reference:
        parts.append(f"creditor ref {instruction.creditor_reference}")
    if (remittance := instruction.remittance) is not None:
        parts.append(
            f"invoice {remittance.invoice_number} dated {remittance.invoice_date.isoformat()} "
            f"for {remittance.invoice_amount_minor / 10**6:.6f}"
        )
    if (fx := instruction.fx) is not None:
        parts.append(
            f"funded with {fx.source_amount_minor / 100:.2f} {fx.source_currency} at {fx.rate:.6f}"
        )
    if instruction.document_sha256 is not None:
        parts.append(f"document sha256 {instruction.document_sha256.hex()[:16]}")
    return ", ".join(parts)
