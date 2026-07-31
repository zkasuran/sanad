/* Sanad web app. Vanilla on purpose: the interesting part of this project is what lands
   on chain, and a build step would only get between a judge and reading it. */

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const usdc = (minor) => (Number(minor) / 1e6).toFixed(6);
const short = (hex, keep = 8) => `${hex.slice(0, 2 + keep)}..${hex.slice(-6)}`;

let CONFIG = null;

const SAMPLE = [
  { address: "0x6a1b4267921f41f9D5D1FACF998Da9BB930701c4", amount: "0.0125", reference: "INV-AE-4101", purpose: "SUPP", uae: "GDI" },
  { address: "0x000000000000000000000000000000000000dEaD", amount: "0.00875", reference: "INV-AE-4102", purpose: "SCVE", uae: "PMS" },
  { address: "0x000000000000000000000000000000000000bEEF", amount: "0.015", reference: "INV-AE-4103", purpose: "GDDS", uae: "GDI" },
  { address: "0x1111111111111111111111111111111111111111", amount: "0.02", reference: "INV-AE-4104", purpose: "SALA", uae: "SAL" },
];

/* ---- tabs ------------------------------------------------------------------ */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    tab.classList.add("active");
    $(`#view-${tab.dataset.view}`).classList.add("active");
  });
});

/* ---- config ---------------------------------------------------------------- */
async function loadConfig() {
  CONFIG = await (await fetch("/api/config")).json();
  $("#chain").textContent = `Arc testnet, chain ${CONFIG.chainId}, block ${CONFIG.latestBlock.toLocaleString()}`;
  $("#foot-block").textContent = `mandate ${CONFIG.mandate}`;
  const rows = [
    ["USDC, the settlement asset and the gas token", CONFIG.usdc],
    ["Memo, attaches the instruction to the payment", CONFIG.memo],
    ["Multicall3From, batches while keeping the payer as sender", CONFIG.multicall3From],
    ["Denylist, the chain's own compliance list", CONFIG.denylist],
    ["Post quantum signature precompile", CONFIG.pqPrecompile],
    ["SanadMandate, ours, anchors the authorization", CONFIG.mandate],
  ];
  const list = el("dl", "kv");
  rows.forEach(([label, address]) => {
    list.append(el("dt", null, label));
    const dd = el("dd");
    dd.append(Object.assign(el("a", null, address), {
      href: `${CONFIG.explorer}/address/${address}`, target: "_blank", rel: "noreferrer",
    }));
    list.append(dd);
  });
  $("#stack").replaceChildren(list);
  $("#stack").classList.remove("empty");
  if (!CONFIG.canSettle) $("#settle").title = "this instance is read only";
  addRows(SAMPLE);
}

/* ---- run builder ----------------------------------------------------------- */
function purposeSelect(codes, selected, cls) {
  const select = el("select", cls);
  Object.entries(codes).forEach(([code, text]) => {
    const option = el("option", null, `${code}  ${text}`);
    option.value = code;
    if (code === selected) option.selected = true;
    select.append(option);
  });
  return select;
}

function addRow(seed = {}) {
  const tr = el("tr");
  const address = el("input", "mono");
  address.value = seed.address || "";
  address.placeholder = "0x…";
  const amount = el("input", "mono");
  amount.value = seed.amount || "";
  amount.placeholder = "0.0100";
  const reference = el("input", "mono");
  reference.value = seed.reference || "";
  reference.placeholder = "INV-AE-0001";
  const iso = purposeSelect(CONFIG.isoPurposeCodes, seed.purpose || "SUPP", "iso");
  const uae = purposeSelect(CONFIG.uaePurposeCodes, seed.uae || "GDI", "uae");
  const kill = el("button", "kill", "×");
  kill.addEventListener("click", () => tr.remove());
  [address, amount, reference, iso, uae, kill].forEach((node) => {
    const td = el("td");
    td.append(node);
    tr.append(td);
  });
  $("#payees tbody").append(tr);
}

function addRows(seeds) {
  $("#payees tbody").replaceChildren();
  seeds.forEach(addRow);
}

function collectRun() {
  const payees = [...$("#payees tbody").children].map((tr) => {
    const [address, amount, reference, iso, uae] = [...tr.querySelectorAll("input, select")];
    return {
      address: address.value.trim(),
      amount_minor: Math.round(Number(amount.value) * 1e6),
      reference: reference.value.trim(),
      purpose: iso.value,
      uae_purpose: uae.value,
      invoice_number: reference.value.trim(),
      invoice_date: new Date().toISOString().slice(0, 10),
    };
  });
  const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 15);
  return { run_id: `RUN-${stamp}`, payees };
}

function renderBytes(hex) {
  const wrap = el("div", "bytes");
  const raw = hex.replace(/^0x/, "");
  for (let i = 0; i < raw.length; i += 2) {
    const byte = parseInt(raw.slice(i, i + 2), 16);
    const printable = byte >= 0x20 && byte <= 0x7e;
    wrap.append(el("span", printable ? "ascii" : "hex", printable ? String.fromCharCode(byte) : raw.slice(i, i + 2)));
  }
  return wrap;
}

/* ---- preflight and settle --------------------------------------------------- */
async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || response.statusText);
  return payload;
}

let PENDING = null;

$("#preview").addEventListener("click", async () => {
  const target = $("#preflight");
  target.classList.remove("empty");
  target.replaceChildren(el("p", null, "screening against Arc's denylist and simulating…"));
  try {
    const run = collectRun();
    const data = await post("/api/runs/preview", run);
    PENDING = run;
    target.replaceChildren();

    const screening = el("div");
    screening.append(el("span", `pill ${data.screening.clean ? "ok" : "bad"}`,
      data.screening.clean ? "denylist clear" : "denylisted payee"));
    screening.append(el("p", "sub", data.screening.explain));
    target.append(screening);

    const kv = el("dl", "kv");
    const feePer = Math.round(data.feeMinor / data.payees);
    [
      ["payees", String(data.payees)],
      ["total", `${usdc(data.totalMinor)} USDC`],
      ["payer balance", `${usdc(data.balanceMinor)} USDC`],
      ["instruction on chain", `${data.instructionBytes} bytes across the run`],
      ["calldata", `${data.calldataBytes} bytes, one transaction`],
      ["mandate digest", data.mandateDigest],
      ["gas", `${data.gas.toLocaleString()} at ${(data.gasPriceWei / 1e9).toFixed(1)} gwei`],
      ["cost", `${usdc(data.feeMinor)} USDC, ${usdc(feePer)} per payee`],
    ].forEach(([label, value]) => {
      kv.append(el("dt", null, label));
      kv.append(el("dd", null, value));
    });
    target.append(kv);

    const sim = el("div");
    sim.append(el("h2", "mt", "simulation, before anything is signed"));
    data.simulation.forEach((entry) => {
      const line = el("div", "pay");
      line.append(el("span", `pill ${entry.success ? "ok" : "bad"}`, entry.success ? "ok" : "would fail"));
      line.append(el("span", "ref", entry.label));
      line.append(el("span", "amt", ""));
      sim.append(line);
    });
    target.append(sim);

    const bytes = el("div");
    bytes.append(el("h2", "mt", "the instruction bytes, as a block explorer shows them"));
    bytes.append(el("p", "sub",
      "Green is readable ASCII. The magic, the ISO 20022 purpose code, the end to end id and the CBUAE code all sit in the clear, so raw hex on Arcscan is still legible to a human."));
    data.lines.forEach((line) => {
      bytes.append(el("div", "ref", `${line.reference}  ${line.memoBytes} bytes  memoId ${short(line.memoId)}`));
      bytes.append(renderBytes(line.memoHex));
    });
    target.append(bytes);

    $("#settle").disabled = !data.screening.clean || !data.simulation.every((s) => s.success) || !CONFIG.canSettle;
  } catch (error) {
    target.replaceChildren(el("p", null, `refused: ${error.message}`));
    $("#settle").disabled = true;
  }
});

$("#settle").addEventListener("click", async () => {
  if (!PENDING) return;
  $("#settle").disabled = true;
  $("#settled-panel").hidden = false;
  $("#settled").replaceChildren(el("p", null, "signing and sending one transaction…"));
  try {
    const data = await post("/api/runs/settle", PENDING);
    const kv = el("dl", "kv");
    [
      ["run", data.runId],
      ["transaction", data.txHash],
      ["block", String(data.blockNumber)],
      ["gas used", data.gasUsed.toLocaleString()],
      ["fee", `${usdc(data.feeMinor)} USDC, ${usdc(data.feePerPayeeMinor)} per payee`],
    ].forEach(([label, value]) => {
      kv.append(el("dt", null, label));
      kv.append(el("dd", null, value));
    });
    const link = Object.assign(el("a", null, "open on Arcscan"), {
      href: data.explorer, target: "_blank", rel: "noreferrer",
    });
    $("#settled").replaceChildren(kv, link);
  } catch (error) {
    $("#settled").replaceChildren(el("p", null, `refused: ${error.message}`));
  }
});

/* ---- audit ------------------------------------------------------------------ */
function renderRun(run) {
  const box = el("div", "run");
  const head = el("div", "run-head");
  head.append(el("span", "run-id", run.runId || short(run.runIdHash)));
  const pills = el("div", "row-actions");
  pills.append(el("span", `pill ${run.complete ? "ok" : "bad"}`, run.complete ? "reconciles" : "does not reconcile"));
  pills.append(el("span", `pill ${run.everyInstructionBinds ? "ok" : "bad"}`,
    run.everyInstructionBinds ? "every instruction binds its transfer" : "an instruction does not bind"));
  pills.append(el("span", `pill ${run.pqVerified ? "ok" : ""}`,
    run.pqVerified ? "post quantum signature verified on chain" : "no post quantum signature offered"));
  head.append(pills);
  box.append(head);

  const who = el("dl", "kv");
  who.append(el("dt", null, "payer"));
  who.append(el("dd", null, run.payer));
  if (run.submitter && run.submitter.toLowerCase() !== run.payer.toLowerCase()) {
    who.append(el("dt", null, "submitted by"));
    who.append(el("dd", null, `${run.submitter}  (paid the gas, never held the funds)`));
  }
  box.append(who);

  const digest = el("div", "digest");
  digest.append(el("span", "label", "anchored on chain"));
  digest.append(el("span", null, run.anchoredDigest));
  digest.append(el("span", "pill", "mandate"));
  digest.append(el("span", "label", "recomputed from events"));
  digest.append(el("span", null, run.recomputedDigest));
  digest.append(el("span", `pill ${run.digestMatches ? "ok" : run.digestRecomputable ? "bad" : "warn"}`,
    run.digestRecomputable ? (run.digestMatches ? "match" : "mismatch") : `rule v${run.digestRule}`));
  box.append(digest);

  run.payments.forEach((payment) => {
    const line = el("div", "pay");
    line.append(el("span", "amt", usdc(payment.amountMinor)));
    const mid = el("div");
    const top = el("div");
    top.append(el("span", "ref", `${payment.reference} → ${payment.payee}`));
    mid.append(top);
    const codes = el("div");
    codes.append(el("span", "pill iso", `${payment.isoPurpose} ${payment.isoPurposeText}`));
    if (payment.uaePurpose) codes.append(el("span", "pill uae", `CBUAE ${payment.uaePurpose} ${payment.uaePurposeText}`));
    if (payment.kind !== "transfer") codes.append(el("span", "pill", payment.kind));
    mid.append(codes);
    line.append(mid);
    line.append(el("span", `pill ${payment.verified ? "ok" : "bad"}`, payment.verified ? "binds" : "unbound"));
    box.append(line);
  });

  if (run.problems.length) {
    const problems = el("ul", "stack-list");
    run.problems.forEach((problem) => problems.append(el("li", null, problem)));
    box.append(problems);
  }
  const link = Object.assign(el("a", null, `transaction ${short(run.txHash)}`), {
    href: run.explorer, target: "_blank", rel: "noreferrer",
  });
  box.append(link);
  return box;
}

async function rebuild() {
  $("#ledger").classList.remove("empty");
  $("#ledger").replaceChildren(el("p", null, "reading Arc. three log queries, then one transaction read per run…"));
  const data = await (await fetch("/api/ledger")).json();

  const verdict = $("#verdict");
  verdict.hidden = false;
  verdict.className = `verdict${data.allRunsReconcile ? "" : " bad"}`;
  verdict.replaceChildren();
  verdict.append(el("b", null, data.allRunsReconcile ? "Every run reconciles against its own mandate. " : "Some runs do not reconcile. "));
  verdict.append(document.createTextNode(
    `${data.runCount} run(s), ${data.paymentCount} payment(s), ${usdc(data.totalMinor)} USDC, rebuilt from blocks ${data.fromBlock.toLocaleString()} to ${data.toBlock.toLocaleString()}. ` +
    `${data.digestVerifiedRuns} of ${data.runCount} had the authorization recomputed from chain data and matched. No database was consulted.`));

  $("#ledger").replaceChildren(...data.runs.map(renderRun));

  const purposes = el("div");
  const isoHead = el("div", "sub", "ISO 20022, the international code");
  purposes.append(isoHead);
  Object.entries(data.byPurpose).forEach(([code, [count, value]]) => {
    const line = el("div", "pay");
    line.append(el("span", "amt", usdc(value)));
    line.append(el("span", "pill iso", code));
    line.append(el("span", "desc", `${count} payment(s)`));
    purposes.append(line);
  });
  purposes.append(el("div", "sub", "CBUAE, the code the Central Bank requires"));
  Object.entries(data.byUaePurpose).forEach(([code, [count, value]]) => {
    const line = el("div", "pay");
    line.append(el("span", "amt", usdc(value)));
    line.append(el("span", "pill uae", code));
    line.append(el("span", "desc", `${count} payment(s)`));
    purposes.append(line);
  });
  $("#purposes").replaceChildren(purposes);
  $("#purposes").classList.remove("empty");

  const parties = el("div");
  Object.entries(data.byCounterparty).forEach(([address, [count, value]]) => {
    const line = el("div", "pay");
    line.append(el("span", "amt", usdc(value)));
    line.append(el("span", "ref", address));
    line.append(el("span", "desc", `${count} settled payment(s)`));
    parties.append(line);
  });
  $("#counterparties").replaceChildren(parties);
  $("#counterparties").classList.remove("empty");
}

$("#rebuild").addEventListener("click", () => rebuild().catch((error) => {
  $("#ledger").replaceChildren(el("p", null, `failed: ${error.message}`));
}));

$("#wipe").addEventListener("click", () => {
  $("#verdict").hidden = true;
  ["#ledger", "#purposes", "#counterparties", "#found"].forEach((sel) => {
    $(sel).replaceChildren(el("p", null, "cleared. everything here can be derived again from Arc alone."));
    $(sel).classList.add("empty");
  });
});

$("#find").addEventListener("click", async () => {
  const reference = $("#reference").value.trim();
  if (!reference) return;
  $("#found").replaceChildren(el("p", "sub", "one indexed topic lookup…"));
  const data = await (await fetch(`/api/ledger/reference/${encodeURIComponent(reference)}`)).json();
  if (!data.found) {
    $("#found").replaceChildren(el("p", "sub", `no payment on Arc carries the reference ${reference}`));
    return;
  }
  const kv = el("dl", "kv");
  [
    ["run", data.runId],
    ["amount", `${usdc(data.payment.amountMinor)} USDC`],
    ["payee", data.payment.payee],
    ["payer", data.payment.payer],
    ["memo id", data.payment.memoId],
    ["instruction", data.payment.describe],
    ["binds its transfer", String(data.payment.verified)],
  ].forEach(([label, value]) => {
    kv.append(el("dt", null, label));
    kv.append(el("dd", null, value));
  });
  $("#found").replaceChildren(kv);
});

$("#add-row").addEventListener("click", () => addRow());
$("#load-sample").addEventListener("click", () => addRows(SAMPLE));

loadConfig().catch((error) => {
  $("#chain").textContent = `cannot reach the API: ${error.message}`;
});
