"""The Sanad API. Build a run, screen it, settle it, then prove it from the chain.

Deliberately thin. Every endpoint is a small translation between JSON and the same pure
functions the scripts use, so there is one implementation of the payment logic and the web
app cannot drift from what the command line does.

The audit endpoints are the interesting ones. `GET /api/ledger` takes no state from this
process at all: it reads Arc and rebuilds every run, every instruction and the arithmetic
that proves each run settled what it authorized. Restart the server, delete its working
directory, point it at a different machine, and the answer is the same.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..arc import addresses
from ..arc.batch import Call3, decode_aggregate3_result, encode_aggregate3
from ..arc.denylist import screen
from ..arc.memo import encode_memo_call
from ..chain import ArcClient
from ..iso20022 import PURPOSE_CODES, PaymentInstruction, StructuredRemittance, describe
from ..ledger import Ledger, PaymentRecord, RunRecord, rebuild
from ..mandate import encode_open, encode_run_header, mandate_address, run_id_hash
from ..payouts import Payee, PayoutError, PayoutRun
from ..uaefts import PURPOSE_CODES as UAE_PURPOSE_CODES

WEB_ROOT = Path(__file__).resolve().parents[3] / "web"

app = FastAPI(title="Sanad", version="0.1.0", docs_url="/api/docs")


def client(*, signing: bool = False) -> ArcClient:
    """A signing client for settlement, a read only one for everything else.

    The read path holding no key is the point, not an accident: an auditor running this
    API needs no ability to move money.
    """
    key = os.getenv("SANAD_PAYER_KEY") or os.getenv("DEPLOYER_PRIVATE_KEY")
    if signing and not key:
        raise HTTPException(503, "this instance is read only, no signing key is configured")
    return ArcClient(key if signing else None)


class PayeeIn(BaseModel):
    address: str
    amount_minor: int = Field(gt=0)
    reference: str
    purpose: str
    uae_purpose: str = ""
    creditor_reference: str = ""
    invoice_number: str = ""
    invoice_date: str = ""
    invoice_amount_minor: int = 0


class RunIn(BaseModel):
    run_id: str
    payees: list[PayeeIn]


def build_run(payload: RunIn, payer: str) -> PayoutRun:
    import datetime as dt

    payees = []
    for row in payload.payees:
        remittance = None
        if row.invoice_number and row.invoice_date:
            remittance = StructuredRemittance(
                invoice_number=row.invoice_number,
                invoice_date=dt.date.fromisoformat(row.invoice_date),
                invoice_amount_minor=row.invoice_amount_minor or row.amount_minor,
            )
        payees.append(
            Payee(
                address=row.address,
                amount_minor=row.amount_minor,
                instruction=PaymentInstruction(
                    end_to_end_id=row.reference,
                    purpose=row.purpose,
                    uae_purpose=row.uae_purpose,
                    creditor_reference=row.creditor_reference,
                    remittance=remittance,
                ),
            )
        )
    return PayoutRun(run_id=payload.run_id, payer=payer, payees=tuple(payees))


def batch_calldata(run: PayoutRun) -> bytes:
    calls = [
        Call3(
            target=addresses.MEMO,
            call_data=encode_memo_call(
                target=mandate_address(),
                data=encode_open(run.run_id, run.mandate_digest(), run.total_minor, len(run.payees)),
                memo_id=run_id_hash(run.run_id),
                memo_data=encode_run_header(run),
            ),
            allow_failure=False,
        ),
        *run.build_calls(allow_failure=True),
    ]
    return encode_aggregate3(calls)


def payment_json(payment: PaymentRecord) -> dict[str, Any]:
    return {
        "memoId": "0x" + payment.memo_id.hex(),
        "reference": payment.reference,
        "isoPurpose": payment.purpose,
        "isoPurposeText": payment.instruction.purpose_description,
        "uaePurpose": payment.instruction.uae_purpose,
        "uaePurposeText": payment.instruction.uae_purpose_description,
        "payee": payment.payee,
        "payer": payment.payer,
        "amountMinor": payment.amount_minor,
        "verified": payment.verified,
        "kind": payment.kind,
        "memoIndex": payment.memo_index,
        "describe": describe(payment.instruction),
        "txHash": payment.tx_hash,
    }


def run_json(run: RunRecord) -> dict[str, Any]:
    return {
        "runId": run.run_id,
        "runIdHash": "0x" + run.run_id_hash.hex(),
        "payer": run.payer,
        "submitter": run.submitter,
        "authorizedPayees": run.payee_count,
        "authorizedMinor": run.total_minor,
        "settledMinor": run.settled_minor,
        "anchoredDigest": "0x" + run.anchored_digest.hex(),
        "recomputedDigest": "0x" + run.recomputed_digest.hex(),
        "digestRule": run.digest_rule,
        "digestRecomputable": run.digest_recomputable,
        "digestMatches": run.digest_matches,
        "everyInstructionBinds": run.every_instruction_binds,
        "pqVerified": run.pq_verified,
        "complete": run.complete,
        "problems": run.problems,
        "txHash": run.tx_hash,
        "explorer": addresses.tx_url(run.tx_hash),
        "blockNumber": run.block_number,
        "payments": [payment_json(payment) for payment in run.payments],
    }


def ledger_json(ledger: Ledger) -> dict[str, Any]:
    return {
        "fromBlock": ledger.from_block,
        "toBlock": ledger.to_block,
        "mandate": mandate_address(),
        "runs": [run_json(run) for run in ledger.runs],
        "byPurpose": {code: list(value) for code, value in ledger.by_purpose().items()},
        "byUaePurpose": {code: list(value) for code, value in ledger.by_uae_purpose().items()},
        "byCounterparty": {code: list(value) for code, value in ledger.by_counterparty().items()},
        "totalMinor": ledger.total_minor,
        "runCount": len(ledger.runs),
        "paymentCount": len(ledger.payments),
        "allRunsReconcile": ledger.all_runs_reconcile,
        "digestVerifiedRuns": ledger.digest_verified_runs,
    }


@app.get("/api/config")
def config() -> dict[str, Any]:
    """Everything the web app needs to render without hardcoding a chain fact."""
    read = client()
    return {
        "chainId": read.chain_id,
        "rpc": addresses.RPC_URL,
        "explorer": addresses.EXPLORER,
        "usdc": addresses.USDC,
        "memo": addresses.MEMO,
        "multicall3From": addresses.MULTICALL3_FROM,
        "denylist": addresses.DENYLIST_TESTNET,
        "pqPrecompile": addresses.PQ,
        "mandate": mandate_address(),
        "latestBlock": int(read.w3.eth.block_number),
        "canSettle": bool(os.getenv("SANAD_PAYER_KEY") or os.getenv("DEPLOYER_PRIVATE_KEY")),
        "isoPurposeCodes": PURPOSE_CODES,
        "uaePurposeCodes": UAE_PURPOSE_CODES,
    }


@app.post("/api/runs/preview")
def preview(payload: RunIn) -> dict[str, Any]:
    """Screen, simulate and price a run without sending anything.

    Three answers a payments operator wants before signing: is any payee on Arc's own
    denylist, would any line fail, and what does the whole thing cost.
    """
    read = client()
    payer_address = os.getenv("SANAD_PAYER_ADDRESS", "")
    if not payer_address:
        try:
            payer_address = client(signing=True).address
        except HTTPException:
            raise HTTPException(503, "no payer address configured") from None
    try:
        run = build_run(payload, payer_address)
    except (PayoutError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    screening = screen(read, [payee.address for payee in run.payees])
    data = batch_calldata(run)
    results = decode_aggregate3_result(read.call(addresses.MULTICALL3_FROM, data, sender=payer_address))
    labels = ["mandate", *[payee.instruction.end_to_end_id for payee in run.payees]]
    gas = read.w3.eth.estimate_gas(
        {"to": addresses.MULTICALL3_FROM, "data": data, "from": payer_address}  # type: ignore[arg-type]
    )
    price = int(read.w3.eth.gas_price)
    return {
        "runId": run.run_id,
        "payer": run.payer,
        "payees": len(run.payees),
        "totalMinor": run.total_minor,
        "balanceMinor": read.balance_minor(payer_address),
        "instructionBytes": run.memo_byte_count,
        "calldataBytes": len(data),
        "mandateDigest": "0x" + run.mandate_digest().hex(),
        "screening": {
            "clean": screening.clean,
            "denylisted": list(screening.denylisted),
            "explain": screening.explain(),
            "denylist": screening.denylist_address,
        },
        "simulation": [
            {"label": label, "success": result.success, "returnData": "0x" + result.return_data.hex()}
            for label, result in zip(labels, results, strict=False)
        ],
        "gas": int(gas),
        "gasPriceWei": price,
        "feeMinor": int(gas) * price // addresses.NATIVE_TO_ERC20_FACTOR,
        "lines": [
            {
                "reference": payee.instruction.end_to_end_id,
                "payee": payee.address,
                "amountMinor": payee.amount_minor,
                "memoId": "0x" + payee.memo_id.hex(),
                "memoBytes": len(payee.memo_bytes),
                "memoHex": "0x" + payee.memo_bytes.hex(),
                "describe": describe(payee.instruction),
            }
            for payee in run.payees
        ],
    }


@app.post("/api/runs/settle")
def settle(payload: RunIn) -> dict[str, Any]:
    """Sign and send. One transaction, the mandate plus every payout."""
    signer = client(signing=True)
    try:
        run = build_run(payload, signer.address)
    except (PayoutError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    screening = screen(signer, [payee.address for payee in run.payees])
    if not screening.clean:
        raise HTTPException(409, screening.explain())

    data = batch_calldata(run)
    results = decode_aggregate3_result(signer.call(addresses.MULTICALL3_FROM, data))
    if not all(result.success for result in results):
        raise HTTPException(409, "a line would fail, refusing to send")

    sent = signer.send(addresses.MULTICALL3_FROM, data)
    return {
        "runId": run.run_id,
        "txHash": sent.tx_hash,
        "explorer": sent.url,
        "blockNumber": sent.block_number,
        "gasUsed": sent.gas_used,
        "feeMinor": sent.fee_minor,
        "feePerPayeeMinor": sent.fee_minor // len(run.payees),
        "status": sent.status,
    }


@app.get("/api/ledger")
def read_ledger(from_block: int | None = None, to_block: str = "latest") -> dict[str, Any]:
    """Rebuild every run from Arc. No database is consulted, because there is not one."""
    import json

    read = client()
    record = json.loads(
        (Path(__file__).resolve().parents[3] / "deployments" / "arc-testnet.json").read_text()
    )
    deploy_block = record["contracts"]["SanadMandate"].get("deployBlock") or 0
    ledger = rebuild(
        read,
        mandate_address=mandate_address(),
        from_block=from_block if from_block is not None else deploy_block,
        to_block=to_block,
    )
    return ledger_json(ledger)


@app.get("/api/ledger/reference/{reference}")
def lookup(reference: str) -> dict[str, Any]:
    """One invoice reference, found by its indexed memo id rather than by a scan."""
    payload = read_ledger()
    for run in payload["runs"]:
        for payment in run["payments"]:
            if payment["reference"] == reference:
                return {"found": True, "runId": run["runId"], "payment": payment}
    return {"found": False, "reference": reference}


@app.get("/api/counterparty/{address}")
def counterparty(address: str) -> dict[str, Any]:
    """A counterparty's whole payment history, which is what a lender reads as credit."""
    payload = read_ledger()
    target = address.lower()
    history = [
        {**payment, "runId": run["runId"], "blockNumber": run["blockNumber"]}
        for run in payload["runs"]
        for payment in run["payments"]
        if payment["payee"].lower() == target
    ]
    return {
        "address": address,
        "payments": history,
        "count": len(history),
        "totalMinor": sum(item["amountMinor"] for item in history),
        "everyPaymentVerified": all(item["verified"] for item in history),
    }


if WEB_ROOT.is_dir():
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
