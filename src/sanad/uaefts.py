"""CBUAE purpose of payment codes, the domestic half of a UAE payment instruction.

A UAE outbound payment does not get to choose whether it carries a purpose code. The
Central Bank of the UAE requires one on every outgoing transfer, in any currency,
through the UAE Funds Transfer System (UAEFTS), and the banks that clear those payments
publish the same table. HSBC UAE: "With effect from 1st September 2018, PoP codes will
become mandatory for all outbound cross border payments initiated from HSBC UAE
accounts." Nordea, on where the code goes: "A 3-letter purpose code should be used for
all payments to UAE, irrespectively of currency. The code must be placed in the first
line of the details of payment / message to the beneficiary field."

Read that last sentence again, because it is the reason this project exists. A field the
central bank mandates has no structured home in the message. It is typed into the first
line of a free text box. A plain USDC transfer does not even have the free text box.

Note this list is NOT ISO 20022. ExternalPurpose1Code is four characters and
international; these are three characters and specific to the UAE. A real cross border
payment out of Dubai carries both, so `sanad.iso20022.PaymentInstruction` carries both
too, and neither is derived from the other.

Provenance: extracted 2026-07-31 from the published tables of HSBC UAE and Nordea, cross
checked against Citi and National Bank of Oman UAE. 112 codes. Descriptions are the
banks' own wording, with two obvious typos in the source corrected ("Interst rate
unwind" and "Own account trnsfer").

The list has real churn, so it is dated rather than treated as eternal: REM, MIS, RBC,
ROC and INV were withdrawn on 10 September 2018, and GDS was withdrawn on 1 December
2018, replaced by GDE for exports and GDI for imports. A payment file built against a
stale table gets rejected by the bank, which is exactly the class of error a closed list
catches at build time.

Sources:
* https://connect-content.us.hsbc.com/hsbc_pcm/onetime/UAE_PoP_List%20Explanatory_For_HSBC_UAE_Website_V1.2_(20180816).pdf
* https://nordea.com/en/doc/united-arab-emirates-purpose-of-payment-codes.pdf
* https://www.citibank.com/tts/docs/AED_Transaction_Type_Codes.pdf
* https://uae.nbo.om/en/Download%20Documents/UAE%20Purpose%20of%20Payment%20Codes.pdf
"""

from __future__ import annotations

from typing import Final

#: Codes withdrawn by the Central Bank. Kept so a stale input can be named rather than
#: just rejected as unknown.
WITHDRAWN: Final[dict[str, str]] = {
    "REM": "withdrawn 2018-09-10",
    "MIS": "withdrawn 2018-09-10",
    "RBC": "withdrawn 2018-09-10",
    "ROC": "withdrawn 2018-09-10",
    "INV": "withdrawn 2018-09-10",
    "GDS": "withdrawn 2018-12-01, use GDE for exports or GDI for imports",
}

PURPOSE_CODES: Final[dict[str, str]] = {
    "ACM": "Agency commissions",
    "AES": "Advance payment against EOS",
    "AFA": "Receipts or payments from personal residents bank account or deposits abroad",
    "AFL": "Receipts or payments from personal non-resident bank account in the UAE",
    "ALW": "Allowance",
    "ATS": "Air transport",
    "BON": "Bonus",
    "CCP": "Corporate card payments",
    "CEA": "merger or acquisition of companies abroad from residents and participation to capital increase of related",
    "CEL": "equity of merger or acquisition of companies in the UAE from non-residents and participation to capital",
    "CHC": "Charitable contributions (charity and aid)",
    "CIN": "Commercial investments",
    "COM": "Commission",
    "COP": "Compensation",
    "CRP": "Credit card payment",
    "DCP": "Debit card payments",
    "DIV": "Dividend payouts from FI",
    "DLA": "Purchases and sales of foreign debt securities in not related companies - more than a year",
    "DLF": "Debt instruments intragroup loans, deposits foreign (above 10% share)",
    "DLL": "Purchases and sales of securities issued by residents in not related companies - more than a year",
    "DOE": "Dividends on equity not intragroup",
    "DSA": "Purchases and sales of foreign debt securities in not related companies - less than a year",
    "DSF": "Debt instruments intragroup foreign securities",
    "DSL": "Purchases and sales of securities issued by residents in not related companies - less than a year",
    "EDU": "Educational support",
    "EMI": "Equated monthly instalments",
    "EOS": "End of service / final settlement",
    "FAM": "Family support (workers' remittances)",
    "FDA": "Financial derivatives foreign",
    "FDL": "Financial derivatives in the UAE",
    "FIA": "Investment fund shares foreign",
    "FIL": "Investment fund shares in the UAE",
    "FIS": "Financial services",
    "FSA": "Equity other than investment fund shares in not related companies abroad",
    "FSL": "Equity other than investment fund shares in not related companies in the UAE",
    "GDE": "Goods sold (exports in FOB value)",
    "GDI": "Goods bought (imports in CIF value)",
    "GMS": "Processing repair and maintenance services on goods",
    "GOS": "Government goods and services embassies, etc.",
    "GRI": "Government related income taxes, tariffs, capital transfers, etc.",
    "IFS": "Information services",
    "IGD": "Dividends intragroup",
    "IGT": "Inter group transfer",
    "IID": "Interest on debt intragroup",
    "INS": "Insurance services",
    "IOD": "Income on deposits",
    "IOL": "Income on loans",
    "IPC": "Charges for the use of intellectual property royalties",
    "IPO": "IPO subscriptions",
    "IRP": "Interest rate swap payments",
    "IRW": "Interest rate unwind payments",
    "ISH": "Income on investment funds shares",
    "ISL": "Interest on securities more than a year",
    "ISS": "Interest on securities less than a year",
    "ITS": "Computer services",
    "LAS": "Leave salary",
    "LDL": "Debt instruments intragroup loans, deposits in the UAE (above 10% share)",
    "LDS": "Debt instruments intragroup securities in the UAE",
    "LEA": "Leasing abroad",
    "LEL": "Leasing in the UAE",
    "LIP": "Loan interest payments",
    "LLA": "Loans - drawings or repayments on loans extended to nonresidents - long-term",
    "LLL": "Loans - drawings or repayments on foreign loans extended to residents - long-term",
    "LNC": "Loan charges",
    "LND": "Loan disbursements from FI",
    "MCR": "Monetary claim reimbursements",
    "MWI": "Mobile wallet card cash-in",
    "MWO": "Mobile wallet card cash-out",
    "MWP": "Mobile wallet card payments",
    "OAT": "Own account transfer",
    "OTS": "Other modes of transport (including postal and courier services)",
    "OVT": "Overtime",
    "PEN": "Pension",
    "PIN": "Personal investments",
    "PIP": "Profits on islamic products",
    "PMS": "Professional and management consulting services",
    "POR": "Refunds/reversals on ipo subscriptions",
    "POS": "POS merchant settlement",
    "PPA": "Purchase of real estate abroad from residents",
    "PPL": "Purchase of real estate in the UAE from non-residents",
    "PRP": "Profit rate swap payments",
    "PRR": "Profits or rents on real estate",
    "PRS": "Personal, cultural, audiovisual and recreational services",
    "PRW": "Profit rate unwind payments",
    "RDA": "Reverse debt instruments abroad",
    "RDL": "Reverse debt instruments in the UAE",
    "RDS": "Research and development services",
    "REA": "Reverse equity share abroad",
    "REL": "Reverse equity share in the UAE",
    "RFS": "Repos on foreign securities",
    "RLS": "Repos on securities issued by residents",
    "RNT": "Rent payments",
    "SAA": "Salary advance",
    "SAL": "Salary (compensation of employees)",
    "SCO": "Construction",
    "SLA": "Loans - drawings or repayments on loans extended to nonresidents - short-term",
    "SLL": "Loans - drawings or repayments on foreign loans extended to residents - short-term",
    "STR": "Travel",
    "STS": "Sea transport",
    "SVI": "Stored value card cash-in",
    "SVO": "Stored value card cash-out",
    "SVP": "Stored value card payments",
    "TAX": "Tax payment",
    "TCP": "Trade credits and advances payable",
    "TCR": "Trade credits and advances receivable",
    "TCS": "Telecommunication services",
    "TKT": "Tickets",
    "TOF": "Transfer of funds between persons normal and juridical",
    "TTS": "Technical, trade-related and other business services",
    "UFP": "Unclaimed funds placement",
    "UTL": "Utility bill payments",
    "XAT": "Tax refund",
}


class UnknownPurposeCode(ValueError):
    """A code the Central Bank's table does not contain, or no longer contains."""


def describe(code: str) -> str:
    """The bank's own wording for a code."""
    validate(code)
    return PURPOSE_CODES[code]


def validate(code: str) -> None:
    """Reject anything a UAE bank would reject, and say why.

    Withdrawn codes get their own message because "GDS is not a code" is unhelpful when
    the answer is "GDS was replaced by GDE and GDI in 2018".
    """
    if code in WITHDRAWN:
        raise UnknownPurposeCode(f"purpose code {code} is {WITHDRAWN[code]}")
    if code not in PURPOSE_CODES:
        raise UnknownPurposeCode(
            f"{code!r} is not a CBUAE purpose of payment code. The table has "
            f"{len(PURPOSE_CODES)} entries, all three uppercase letters"
        )


def search(term: str) -> dict[str, str]:
    """Find codes by description. A payments operator knows "salary", not "SAL"."""
    needle = term.lower()
    return {
        code: text for code, text in PURPOSE_CODES.items() if needle in text.lower()
    }
