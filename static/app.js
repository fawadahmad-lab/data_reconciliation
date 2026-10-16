const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const F = ["date", "grn_no", "supplier", "item", "lot", "qty", "unit", "recd_by"];
const HEAD = { date: "Date", grn_no: "GRN", supplier: "Supplier", item: "Item", lot: "Lot", qty: "Qty", unit: "Unit", recd_by: "By" };
const FLAGF = { date_illegible: "date", date_unexpected: "date", grn_illegible: "grn_no", lot_illegible: "lot", lot_format: "lot",
  qty_illegible: "qty", uncertain_digit: "qty", unit_unknown: "unit" };
const NAME = { register: "Paper register", erp: "ERP", store: "Store sheet" };
const FIELD = { lot: "Lot", date: "Date", qty_uom: "Quantity", supplier: "Supplier", item: "Item" };
const ENG = { rapidocr: "RapidOCR", "saved reading": "a saved reading", manual: "nobody (type it in)" };
let doc = { month: "", pages: [] }, useSample = false, urls = [], sel = "A", result = null;

const msg = (el, text, kind) => { $(el).innerHTML = text ? `<div class="msg ${kind || ""}">${text}</div>` : ""; };
async function api(url, opts) {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || "Something went wrong (" + r.status + ")");
  return j;
}

fetch("/api/health").then(r => r.json()).then(h => {
  $("#engines").textContent = `RapidOCR: ${h.rapidocr ? "ready" : "not installed"}`;
}).catch(() => {});

// ---------- 1. read
function refLine(s) {
  if (!s) return "";
  if (!s.applied) return `<div class="msg warn">Reference check not applied: ${esc(s.reason)}.</div>`;
  return `<div class="msg ok">Checked against the reference page ${s.reference_page}: OCR read ${s.exact} of ${s.cells} cells exactly right. ${s.corrected} cells corrected (blue)${s.filled ? `, ${s.filled} rows OCR missed were filled in` : ""}${s.not_in_reference ? `, ${s.not_in_reference} rows not found in the reference` : ""}.</div>`;
}
function renderPages() {
  if (doc.month) $("#month").value = doc.month;
  $("#pages").innerHTML = doc.pages.map((p, pi) => {
    const fb = (p.log || []).filter(l => !l.ok).map(l => `${ENG[l.engine] || l.engine} could not read it (${esc(l.why)})`);
    const note = (p.log || []).filter(l => l.ok && l.why).map(l => esc(l.why));
    const rows = p.rows.length, unclear = p.rows.filter(r => (r.flags || []).some(f => FLAGF[f])).length;
    return `<div class="page">${urls[pi] ? `<img src="${urls[pi]}" alt="page ${p.page}">` : ""}<div style="flex:1;min-width:0">
      <b>Page ${p.page}</b> &middot; read by ${ENG[p.engine] || p.engine}. ${rows} rows, ${unclear} with unclear cells.
      ${fb.length ? `<div class="msg warn">${fb.join("<br>")}<br>Please type the rows in.</div>` : ""}
      ${note.length ? `<div class="mute">${note.join(" ")}</div>` : ""}
      ${refLine(p.reference)}
      <div class="scroll"><table><tr>${F.map(f => `<th>${HEAD[f]}</th>`).join("")}</tr>
      ${p.rows.map((r, ri) => `<tr class="${r.source === "filled" ? "filled" : ""}">${F.map(f => {
        const bad = (r.flags || []).some(fl => FLAGF[fl] === f), fx = (r.corrections || []).find(c => c.field === f);
        const tip = bad ? (r.note || "Unclear: please check") : fx ? `OCR read: ${fx.ocr ?? "nothing"}. Corrected from the reference.` : "";
        return `<td><input data-p="${pi}" data-r="${ri}" data-f="${f}" class="${bad ? "warn" : fx ? "fix" : ""}" value="${esc(r[f])}" ${tip ? `title="${esc(tip)}"` : ""}></td>`;
      }).join("")}</tr>`).join("")}</table></div>
      <button data-add="${pi}">Add a row</button></div></div>`;
  }).join("");
}
$("#pages").addEventListener("input", e => {
  const d = e.target.dataset;
  if (d.f === undefined) return;
  const row = doc.pages[+d.p].rows[+d.r];
  row[d.f] = e.target.value;
  row.flags = (row.flags || []).filter(fl => FLAGF[fl] !== d.f);   // a person has checked this cell
  row.corrections = (row.corrections || []).filter(c => c.field !== d.f);
  e.target.classList.remove("warn", "fix");
});
$("#pages").addEventListener("click", e => {
  if (e.target.dataset.add === undefined) return;
  const p = doc.pages[+e.target.dataset.add];
  p.rows.push({ row: p.rows.length + 1, flags: [], note: null, ...Object.fromEntries(F.map(f => [f, ""])) });
  renderPages();
});
async function read(sample) {
  const fd = new FormData();
  urls = [];
  if (sample) fd.append("sample", "true");
  else {
    const files = [...$("#pics").files];
    if (!files.length) return msg("#readStatus", "Add at least one photo first.", "bad");
    files.forEach(f => { fd.append("files", f); urls.push(URL.createObjectURL(f)); });
  }
  fd.append("simulate", $("#sim").checked ? "true" : "false");
  fd.append("reference", $("#ref").checked ? "true" : "false");
  fd.append("cache", $("#fresh").checked ? "false" : "true");
  useSample = sample;
  $("#read").disabled = $("#sample").disabled = true;
  msg("#readStatus", "Reading the pages. This can take a minute.");
  try {
    doc = await api("/api/read", { method: "POST", body: fd });
    if (sample) urls = ["/sample/t4_register_page1.jpg", "/sample/t4_register_page2.jpg"];
    const total = doc.pages.reduce((n, p) => n + p.rows.length, 0), bad = doc.pages.reduce((n, p) => n + p.rows.filter(r => (r.flags || []).some(f => FLAGF[f])).length, 0);
    msg("#readStatus", `${total} rows read. ${bad ? `${bad} have unclear cells (amber).` : "Nothing unclear."} Check the table, then compare.`, bad ? "warn" : "ok");
    renderPages();
  } catch (e) { msg("#readStatus", e.message, "bad"); }
  $("#read").disabled = $("#sample").disabled = false;
}
$("#read").onclick = () => read(false);
$("#sample").onclick = () => read(true);

// ---------- 2. compare
$("#compare").onclick = async () => {
  if (!doc.pages.length) return msg("#cmpStatus", "Read the register first (step 1).", "bad");
  const fd = new FormData();
  doc.month = $("#month").value;
  fd.append("register", JSON.stringify(doc));
  const sampleFiles = useSample && !$("#erp").files[0] && !$("#store").files[0];
  fd.append("sample", sampleFiles ? "true" : "false");
  if ($("#erp").files[0]) fd.append("erp", $("#erp").files[0]);
  if ($("#store").files[0]) fd.append("store", $("#store").files[0]);
  $("#compare").disabled = true;
  msg("#cmpStatus", "Comparing...");
  try { result = await api("/api/compare", { method: "POST", body: fd }); msg("#cmpStatus", ""); sel = "A"; renderResult(); }
  catch (e) { msg("#cmpStatus", e.message, "bad"); }
  $("#compare").disabled = false;
};

// ---------- 3. result
const cell = (v, odd) => `<td class="${odd ? "odd" : ""}">${v == null ? '<span class="miss">not recorded</span>' : esc(v)}</td>`;
function renderResult() {
  const r = result, c = r.counts;
  $("#res").hidden = false;
  $("#cards").innerHTML = [["A", "Matched", c.A], ["B", "Mismatched", c.B], ["C", "Missing", c.C]].map(([k, t, n]) =>
    `<button class="card ${k} ${sel === k ? "sel" : ""}" data-k="${k}"><strong>${n}</strong>${t}</button>`).join("");
  const eq = r.lines.map(l => `<div class="eq">${NAME[l.source].padEnd(14, " ").replace(/ /g, "&nbsp;")} ${l.rows} rows = ${l.A} matched + ${l.B} mismatched + ${l.C} missing</div>`).join("");
  $("#proof").innerHTML = `<div class="msg ${r.ok ? "ok" : "bad"}"><b>${r.ok ? `PROOF OK: ${r.total} rows read, all ${r.total} accounted for.` : "NOT PROVEN: some rows are not accounted for."}</b>${eq}
    ${r.gaps.length ? `<div>Delivery number ${r.gaps.map(g => "GRN-" + g).join(", ")} is in none of the three sources.</div>` : ""}</div>
    <details><summary>Show every row and the quantity checks</summary>
      <div class="scroll"><table><tr><th>Check</th><th>Expected</th><th>Actual</th><th></th></tr>${r.checks.map(k => `<tr><td>${esc(k.name)}</td><td>${k.expected}</td><td>${k.actual}</td><td class="${k.ok ? "tick" : "miss"}">${k.ok ? "OK" : "FAIL"}</td></tr>`).join("")}</table></div>
      <div class="scroll"><table><tr><th>Source</th><th>Row</th><th>GRN</th><th>Result</th></tr>${r.ledger.map(l => `<tr><td>${NAME[l.source]}</td><td>${esc(l.row)}</td><td>${l.grn}</td><td>${{ A: "Matched", B: "Mismatched", C: "Missing" }[l.group]}</td></tr>`).join("")}</table></div></details>`;
  const d = $("#detail");
  if (sel === "A") d.innerHTML = `<h3>Matched: the three sources agree on every field</h3><div class="scroll"><table><tr><th>GRN</th><th>Item</th><th>Date</th><th>Lot</th><th>Qty register</th><th>Qty ERP</th><th>Qty store</th><th></th></tr>
    ${r.A.map(a => `<tr><td>${a.id.replace("GRN-", "")}</td><td>${esc(a.item)}</td><td>${a.date}</td><td>${a.lot}</td><td>${esc(a.qty.register)}</td><td>${esc(a.qty.erp)}</td><td>${esc(a.qty.store)}</td><td class="tick">&#10003;</td></tr>`).join("")}</table></div>
    <h3>Matched quantity totals</h3><div class="scroll"><table><tr><th>Unit</th><th>Register</th><th>ERP</th><th>Store</th><th>Difference</th></tr>
    ${r.totals.map(t => `<tr><td>${t.unit}</td><td>${t.register}</td><td>${t.erp}</td><td>${t.store}</td><td class="tick">${Math.max(t.register, t.erp, t.store) - Math.min(t.register, t.erp, t.store)}</td></tr>`).join("")}</table></div>`;
  if (sel === "B") d.innerHTML = `<h3>Mismatched: where they disagree</h3><div class="scroll"><table><tr><th>GRN</th><th>Item</th><th>Field</th><th>Paper register</th><th>ERP</th><th>Store sheet</th><th>Note</th></tr>
    ${r.B.map(b => `<tr><td>${b.id.replace("GRN-", "")}</td><td>${esc(b.item)}</td><td>${FIELD[b.field] || b.field}</td>${["register", "erp", "store"].map(s =>
      b.blank === s ? `<td class="miss">can't read it</td>` : cell(b.values[s], b.odd === s)).join("")}<td>${esc(b.note)}</td></tr>`).join("")}</table></div>`;
  if (sel === "C") d.innerHTML = `<h3>Missing: not found in every source</h3><div class="scroll"><table><tr><th>Item</th><th>What</th><th>Register</th><th>ERP</th><th>Store</th><th>Detail</th></tr>
    ${r.C.map(m => `<tr><td>${esc(String(m.id).replace("GRN-", ""))}</td><td>${esc(m.label)}</td>${["register", "erp", "store"].map(s =>
      `<td class="${m.present[s] ? "tick" : "miss"}">${m.present[s] ? "&#10003;" : "&#10007;"}</td>`).join("")}<td>${esc(m.note)}</td></tr>`).join("")}</table></div>`;
}
$("#cards").addEventListener("click", e => { const b = e.target.closest("[data-k]"); if (b) { sel = b.dataset.k; renderResult(); } });
$("#assump").onclick = e => { e.preventDefault(); $("#assumptions").hidden = !$("#assumptions").hidden; };
