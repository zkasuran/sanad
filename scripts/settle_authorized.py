"""Settle a run the way a treasury would: the payer authorizes, an operator submits.

    python scripts/settle_authorized.py            # dry run and simulation
    python scripts/settle_authorized.py --send
    SANAD_SIGNER=circle python scripts/settle_authorized.py --send

The payer never broadcasts a transaction and never pays gas. It signs one EIP-3009
authorization per payout, and an operator batches those through Arc's Memo and
Multicall3From. The USDC `Transfer` events still name the payer, because
`transferWithAuthorization` takes the sender from the signature rather than from
`msg.sender`, so custody and attribution both stay with the payer while the operational
burden moves to the operator.

With `SANAD_SIGNER=circle` the payer's key is a Circle developer controlled wallet and
this process never holds it. That is the configuration a corporate treasury would actually
accept, and it is the reason the authorization is a separate step rather than a `transfer`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sanad.arc import addresses  # noqa: E402
from sanad.arc.batch import Call3, decode_aggregate3_result, encode_aggregate3  # noqa: E402
from sanad.arc.denylist import screen  # noqa: E402
from sanad.arc.memo import encode_memo_call  # noqa: E402
from sanad.chain import ArcClient  # noqa: E402
from sanad.circle.eip3009 import (  # noqa: E402
    authorization_typed_data,
    encode_transfer_with_authorization,
    new_nonce,
)
from sanad.circle.signers import signer_from_env  # noqa: E402
from sanad.iso20022 import PaymentInstruction, StructuredRemittance, describe  # noqa: E402
from sanad.mandate import encode_open, encode_run_header, mandate_address, run_id_hash  # noqa: E402
from sanad.payouts import Payee, PayoutRun  # noqa: E402

RUN_ID = f"AUTH-{dt.datetime.now(tz=dt.timezone.utc):%Y%m%d-%H%M%S}"
TODAY = dt.datetime.now(tz=dt.timezone.utc).date()


def build_run(payer: str) -> PayoutRun:
    address = Web3.to_checksum_address
    return PayoutRun(
        run_id=RUN_ID,
        payer=payer,
        payees=(
            Payee(
                address("0x000000000000000000000000000000000000dead"),
                4_000,
                PaymentInstruction(
                    end_to_end_id=f"{RUN_ID}-01",
                    purpose="SUPP",
                    uae_purpose="GDI",
                    creditor_reference="SUP-IN-018",
                    remittance=StructuredRemittance(f"{RUN_ID}-01", TODAY, 4_000),
                ),
            ),
            Payee(
                address("0x000000000000000000000000000000000000beef"),
                6_000,
                PaymentInstruction(
                    end_to_end_id=f"{RUN_ID}-02",
                    purpose="SCVE",
                    uae_purpose="PMS",
                    creditor_reference="CTR-VN-004",
                ),
            ),
        ),
    )


def main() -> int:
    load_dotenv(ROOT / ".env")
    load_dotenv(Path("/home/asuran/Downloads/hackathon-hq/work/moonwalk/.env"))

    relayer_key = os.environ.get("SANAD_RELAYER_KEY") or os.environ["DEPLOYER_PRIVATE_KEY"]
    os.environ.setdefault("SANAD_PAYER_KEY", os.environ.get("AGENT_PRIVATE_KEY", ""))
    payer_signer = signer_from_env()
    client = ArcClient(relayer_key)

    run = build_run(payer_signer.address)
    print(f"run       {run.run_id}")
    print(f"payer     {payer_signer.address}  (key backend: {payer_signer.backend})")
    print(f"operator  {client.address}  (submits and pays the gas)")
    print(f"total     {run.total:.6f} USDC over {len(run.payees)} payout(s)")
    print(f"payer USDC balance {client.balance_minor(payer_signer.address) / 10**6:.6f}")
    print(f"payer transaction count {client.w3.eth.get_transaction_count(payer_signer.address)}")

    result = screen(client, [payee.address for payee in run.payees])
    print(f"\nscreening {result.explain()}")
    if not result.clean:
        return 1

    calls: list[Call3] = [
        Call3(
            target=addresses.MEMO,
            call_data=encode_memo_call(
                target=mandate_address(),
                data=encode_open(
                    run.run_id,
                    run.mandate_digest(),
                    run.total_minor,
                    len(run.payees),
                    payer=payer_signer.address,
                ),
                memo_id=run_id_hash(run.run_id),
                memo_data=encode_run_header(run),
            ),
            allow_failure=False,
        )
    ]
    authorizations: list[dict[str, object]] = []
    for payee in run.payees:
        nonce = new_nonce()
        typed = authorization_typed_data(
            payer=payer_signer.address, payee=payee.address, value=payee.amount_minor, nonce=nonce
        )
        signature = payer_signer.sign_typed_data(typed)
        recovered = Account.recover_message(
            encode_typed_data(full_message=typed), signature=signature
        )
        if recovered.lower() != payer_signer.address.lower():
            print(f"signature does not recover to the payer: {recovered}", file=sys.stderr)
            return 1
        inner = encode_transfer_with_authorization(typed, signature)
        calls.append(
            Call3(
                target=addresses.MEMO,
                call_data=encode_memo_call(
                    target=run.token,
                    data=inner,
                    memo_id=payee.memo_id,
                    memo_data=payee.memo_bytes,
                ),
                allow_failure=True,
            )
        )
        authorizations.append(
            {
                "reference": payee.instruction.end_to_end_id,
                "payee": payee.address,
                "amountMinor": payee.amount_minor,
                "nonce": "0x" + nonce.hex(),
                "validBefore": typed["message"]["validBefore"],
                "signature": "0x" + signature.hex(),
                "recoveredSigner": recovered,
            }
        )
        print(f"  authorized {payee.amount:.6f} USDC -> {payee.address}")
        print(f"             {describe(payee.instruction)}")

    data = encode_aggregate3(calls)
    print(f"\ncalldata  {len(data)} bytes, {len(calls)} entries")
    preview = decode_aggregate3_result(client.call(addresses.MULTICALL3_FROM, data))
    for index, entry in enumerate(preview):
        label = "mandate" if index == 0 else str(authorizations[index - 1]["reference"])
        print(f"  simulate {label:<26} {'ok' if entry.success else 'WOULD FAIL ' + entry.return_data.hex()}")
    if not all(entry.success for entry in preview):
        return 1
    gas = client.estimate(addresses.MULTICALL3_FROM, data)
    print(f"estimate  {gas} gas")

    if "--send" not in sys.argv:
        print("\ndry run. add --send to settle.")
        return 0

    sent = client.send(addresses.MULTICALL3_FROM, data, gas=int(gas * 1.25))
    transfers = [
        log
        for log in sent.logs
        if str(log["address"]).lower() == addresses.USDC.lower()
        and ("0x" + log["topics"][0].hex()) == addresses.TOPIC_TRANSFER
    ]
    print(f"\nsettled   {sent.tx_hash}")
    print(f"          {sent.url}")
    print(f"          block {sent.block_number}, {sent.gas_used} gas, {sent.fee:.9f} USDC of gas")
    print(f"          paid by the operator, not the payer")
    for log in transfers:
        sender = "0x" + log["topics"][1].hex()[-40:]
        recipient = "0x" + log["topics"][2].hex()[-40:]
        amount = int(log["data"].hex() if isinstance(log["data"], bytes) else log["data"], 16)
        mark = "payer" if sender.lower() == payer_signer.address.lower() else "NOT THE PAYER"
        print(f"          USDC {amount / 10**6:.6f} from {sender} ({mark}) to {recipient}")
    print(f"\npayer transaction count after settlement: "
          f"{client.w3.eth.get_transaction_count(payer_signer.address)}")

    out = ROOT / "evidence" / f"authorized-{run.run_id}.json"
    out.write_text(
        json.dumps(
            {
                "runId": run.run_id,
                "signerBackend": payer_signer.backend,
                "payer": payer_signer.address,
                "operator": client.address,
                "txHash": sent.tx_hash,
                "explorer": sent.url,
                "blockNumber": sent.block_number,
                "gasUsed": sent.gas_used,
                "gasFeeUsdc": round(sent.fee, 9),
                "payerTransactionCount": client.w3.eth.get_transaction_count(payer_signer.address),
                "authorizations": authorizations,
                "usdcTransfersFromPayer": sum(
                    1
                    for log in transfers
                    if ("0x" + log["topics"][1].hex()[-40:]).lower() == payer_signer.address.lower()
                ),
                "denylistScreening": result.explain(),
            },
            indent=1,
        )
        + "\n"
    )
    print(f"evidence  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
