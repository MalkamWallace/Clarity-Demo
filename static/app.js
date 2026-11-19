/* Clarity Demo frontend: plain JS, no build step. */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const money = (v, d = 1) => (v == null ? "–" : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}M`);
  const num = (v, d = 1) => (v == null ? "–" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (v, d = 1, sign = true) => {
    if (v == null) return "–";
    const t = (v * 100).toFixed(d);
    const z = Number(t) === 0;
    return `${sign && v > 0 && !z ? "+" : ""}${z ? (0).toFixed(d) : t}%`;
  };
  const cls = (v) => (v == null || Math.abs(v) < 1e-9 ? "" : v >= 0 ? "pos" : "neg");
  const COST = ["COGS", "Opex"];
  const fav = (v, line) => cls(COST.includes(line) && v != null ? -v : v);  // costs going up are unfavorable

  // ------------------------------------------------------------ routing
  const views = ["home", "check", "forecast"];
  function route() {
    const r = views.includes(location.hash.slice(1)) ? location.hash.slice(1) : "home";
    views.forEach((v) => ($(`#view-${v}`).hidden = v !== r));
    $$(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.route === r));
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", route);
  route();

  async function api(path, form) {
    const res = await fetch(path, { method: form ? "POST" : "GET", body: form });
    if (!res.ok) {
      let msg = `Request failed (${res.status})`;
      try { msg = (await res.json()).detail || msg; } catch (_) { /* not json */ }
      throw new Error(msg);
    }
    return res;
  }
  function busy(btn, on) { btn.classList.toggle("loading", on); btn.disabled = on; }

  // ------------------------------------------------------------ catalogue
  let catalogue = null;
  api("/api/catalogue").then((r) => r.json()).then((c) => {
    catalogue = c;
    $("#fc-bu").innerHTML = ["Total", ...c.business_units].map((b) => `<option value="${esc(b)}">${b === "Total" ? "Total company (all BUs)" : esc(b)}</option>`).join("");
    $("#fc-gp").innerHTML = [...c.guiding_principles].reverse().map((y) => `<option value="${y}">${y} budget targets</option>`).join("");
  });

  // ============================================================ CHECK
  let basis = "yoy";
  let lastCheck = null;
  const fileInput = $("#check-file");

  $$('input[name="src"]').forEach((r) => r.addEventListener("change", () => {
    if (r.value === "upload" && r.checked) fileInput.click();
  }));
  fileInput.addEventListener("change", () => {
    $("#check-file-name").textContent = fileInput.files[0] ? fileInput.files[0].name : "";
  });
  $$("#basis button").forEach((b) => b.addEventListener("click", () => {
    $$("#basis button").forEach((x) => x.classList.toggle("on", x === b));
    basis = b.dataset.v;
    if (lastCheck) runCheck();
  }));

  function checkForm() {
    const src = $('input[name="src"]:checked').value;
    const f = new FormData();
    if (src === "upload") {
      if (!fileInput.files[0]) throw new Error("Choose an .xlsx file to upload.");
      f.append("file", fileInput.files[0]);
    } else f.append("sample", src);
    f.append("basis", basis);
    return f;
  }

  async function runCheck() {
    const btn = $("#run-check");
    const out = $("#check-results");
    busy(btn, true);
    try {
      const data = await (await api("/api/check", checkForm())).json();
      lastCheck = data;
      renderCheck(data);
      $("#export-check").disabled = false;
    } catch (e) {
      out.innerHTML = `<div class="error">${esc(e.message)}</div>`;
    } finally { busy(btn, false); }
  }
  $("#run-check").addEventListener("click", runCheck);

  $("#export-check").addEventListener("click", async () => {
    const btn = $("#export-check");
    busy(btn, true);
    try {
      const res = await api("/api/check/export", checkForm());
      const blob = await res.blob();
      const name = (res.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1] || "clarity_review.xlsx";
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { alertBox(e.message); } finally { busy(btn, false); }
  });
  function alertBox(m) { $("#check-results").insertAdjacentHTML("afterbegin", `<div class="error">${esc(m)}</div>`); }

  const BASIS = { yoy: "YoY", qoq: "QoQ", plan: "vs Plan" };

  function renderCheck(d) {
    const s = d.summary;
    const hi = s.by_severity.high, med = s.by_severity.medium;
    const revMismatch = s.shown_total_revenue != null && Math.abs(s.shown_total_revenue - s.revenue) > 0.15;

    const kpis = `
      <div class="kpis">
        <div class="kpi ${s.formula_accuracy < 1 ? "bad" : "good"}"><div class="k-label">Formula accuracy</div>
          <div class="k-val">${(s.formula_accuracy * 100).toFixed(1)}%</div><div class="k-sub">${s.formula_cells} formula cells recalculated</div></div>
        <div class="kpi ${hi ? "bad" : med ? "warn" : "good"}"><div class="k-label">Issues to review</div>
          <div class="k-val">${s.issues}</div><div class="k-sub">${hi} high · ${med} medium</div></div>
        <div class="kpi ${revMismatch ? "bad" : ""}"><div class="k-label">Revenue outlook</div>
          <div class="k-val">${money(s.revenue)}</div><div class="k-sub">${revMismatch ? `Sheet shows ${money(s.shown_total_revenue)}` : `${pct(s.revenue_yoy)} YoY`}</div></div>
        <div class="kpi"><div class="k-label">Gross margin</div>
          <div class="k-val">${pct(s.gm_pct, 1, false)}</div><div class="k-sub">Recalculated from BUs</div></div>
        <div class="kpi"><div class="k-label">Operating income</div>
          <div class="k-val">${money(s.operating_income)}</div><div class="k-sub">${esc(d.outlook_quarter || "")} outlook</div></div>
      </div>`;

    // issues checklist
    const cats = Object.entries(s.by_category);
    const issuesHtml = d.issues.length ? `
      <div class="chips" id="cat-chips">
        <button class="chip on" data-c="*">All<span class="n">${d.issues.length}</span></button>
        ${cats.map(([c, n]) => `<button class="chip" data-c="${esc(c)}">${esc(c)}<span class="n">${n}</span></button>`).join("")}
      </div>
      <ul class="issues" id="issue-list" style="margin-top:10px">
        ${d.issues.map((i, k) => `
          <li class="issue sev-${i.severity}" data-cats="${esc(i.categories.join("|"))}">
            <input type="checkbox" aria-label="Mark reviewed" data-k="${k}">
            <span class="cell-tag">${esc(i.cell)}</span>
            <div>
              <div><span class="i-title">${esc(i.title)}</span>
                <span class="badges">${i.categories.map((c) => `<span class="badge ${catColor(c)}">${esc(c)}</span>`).join("")}</span></div>
              <div class="i-where">${esc(i.bu)} · ${esc(i.line)}</div>
              ${i.details.map((t) => `<p class="i-detail">${fmtDetail(t)}</p>`).join("")}
            </div>
          </li>`).join("")}
      </ul>` : `<div class="all-clear">✓ No formula, tie-out, history or budget issues found. This book is ready for review.</div>`;

    // movers
    const maxScore = Math.max(...d.movers.map((m) => m.score), 1e-9);
    const moversHtml = `
      <div class="tbl-wrap"><table>
        <thead><tr><th>#</th><th>Business unit</th><th>Line</th><th class="num">Outlook</th><th class="num">${BASIS[d.basis]} $</th>
          <th class="num">${BASIS[d.basis]} %</th><th class="num">vs Plan</th><th>Blend score</th></tr></thead>
        <tbody>${d.movers.map((m, k) => `
          <tr><td>${k + 1}</td><td>${esc(m.bu)}</td><td>${esc(m.line)}${m.anomaly ? ' <span class="badge red">anomaly</span>' : ""}</td>
            <td class="num">${money(m.value)}</td><td class="num ${fav(m.change, m.line)}">${m.change >= 0 ? "+" : "−"}${money(Math.abs(m.change))}</td>
            <td class="num ${fav(m.pct, m.line)}">${pct(m.pct)}</td>
            <td class="num ${fav(m.vs_plan, m.line)}">${m.vs_plan == null ? "–" : (m.vs_plan >= 0 ? "+" : "−") + money(Math.abs(m.vs_plan))}</td>
            <td><div class="bar"><i style="width:${(m.score / maxScore) * 100}%"></i></div></td></tr>`).join("")}
        </tbody></table></div>`;

    const c = d.commentary;
    const commHtml = `
      <div class="commentary">
        <div class="card-head"><h2>Slide-ready commentary</h2>
          <span class="muted">${c.mode === "ai" ? "Written by Claude from computed figures" : "Template mode (add an API key for AI-written commentary)"}
          <button class="btn btn-copy" id="copy-comm">Copy</button></span></div>
        <ol>${c.bullets.map((b) => `<li>${esc(b)}</li>`).join("")}</ol>
      </div>`;

    // anomaly table
    const anoms = d.anomalies.filter((a) => ["Revenue", "COGS", "Opex", "Units (K)", "Headcount"].includes(a.line));
    const anomHtml = anoms.length ? `
      <details class="models"><summary>Show the history check for all ${anoms.length} input lines</summary>
      <div class="tbl-wrap" style="margin-top:10px"><table class="lb">
        <thead><tr><th>Business unit</th><th>Line</th><th class="num">Outlook YoY</th><th class="num">Typical range (FY22–FY26)</th>
          <th class="num">Robust z</th><th>Isolation forest</th><th>Result</th></tr></thead>
        <tbody>${anoms.map((a) => `<tr><td>${esc(a.bu)}</td><td>${esc(a.line)}</td><td class="num">${pct(a.yoy)}</td>
          <td class="num">${pct(a.typical_low, 0)} to ${pct(a.typical_high, 0)}</td><td class="num">${a.z.toFixed(1)}</td>
          <td>${a.forest_outlier ? "outlier" : "normal"}</td>
          <td>${a.flagged ? '<span class="badge red">flagged</span>' : '<span class="badge green">ok</span>'}</td></tr>`).join("")}
        </tbody></table></div></details>` : `<p class="muted">${esc(d.history_note || "")}</p>`;

    $("#check-results").innerHTML = `
      ${kpis}
      <div class="card">
        <div class="card-head"><h2>Review checklist</h2><span class="muted">${esc(d.file)} · tick items off as you fix them</span></div>
        ${issuesHtml}
      </div>
      ${commHtml}
      <div class="card">
        <div class="card-head"><h2>Top ${d.movers.length} key movers</h2><span class="muted">Ranked on a 50/50 blend of $ change and % change, ${BASIS[d.basis]}</span></div>
        ${moversHtml}
      </div>
      <div class="card">
        <div class="card-head"><h2>Anomaly detection</h2><span class="muted">Outlook vs FY22–FY26 history · plan threshold ±${(d.threshold * 100).toFixed(0)}% (${esc(d.principles_fy)} guiding principles)</span></div>
        ${anomHtml}
      </div>`;

    $$("#cat-chips .chip").forEach((ch) => ch.addEventListener("click", () => {
      $$("#cat-chips .chip").forEach((x) => x.classList.toggle("on", x === ch));
      $$("#issue-list .issue").forEach((li) => {
        li.hidden = ch.dataset.c !== "*" && !li.dataset.cats.split("|").includes(ch.dataset.c);
      });
    }));
    $$("#issue-list input").forEach((cb) => cb.addEventListener("change", () => cb.closest(".issue").classList.toggle("done", cb.checked)));
    $("#copy-comm").addEventListener("click", (e) => {
      navigator.clipboard?.writeText(c.bullets.map((b) => `• ${b}`).join("\n"));
      e.target.textContent = "Copied";
      setTimeout(() => (e.target.textContent = "Copy"), 1500);
    });
  }

  function catColor(c) {
    if (["Broken formula", "Does not tie", "Repeated calculation", "Inconsistent formula", "Hardcoded value"].includes(c)) return "red";
    if (c === "Anomaly vs history" || c === "Budget variance") return "amber";
    return "";
  }
  function fmtDetail(t) {
    return esc(t).replace(/(=[A-Z]+\([^)]*\)[^\s]*|=[A-Z]+\d+[^\s,.]*)/g, "<code>$1</code>");
  }

  // ============================================================ FORECAST
  const fcFile = $("#fc-file");
  $("#fc-source").addEventListener("change", (e) => { if (e.target.value === "upload") fcFile.click(); });
  fcFile.addEventListener("change", () => { $("#fc-file-name").textContent = fcFile.files[0]?.name || ""; });
  $("#fc-h").addEventListener("input", (e) => ($("#fc-h-out").textContent = e.target.value));

  let lastFc = null;
  let chartLine = "Revenue";

  $("#run-fc").addEventListener("click", async () => {
    const btn = $("#run-fc");
    busy(btn, true);
    try {
      const f = new FormData();
      f.append("bu", $("#fc-bu").value);
      f.append("horizon", $("#fc-h").value);
      f.append("principles_fy", $("#fc-gp").value);
      if ($("#fc-source").value === "upload") {
        if (!fcFile.files[0]) throw new Error("Choose a history CSV to upload.");
        f.append("file", fcFile.files[0]);
      }
      lastFc = await (await api("/api/forecast", f)).json();
      renderForecast(lastFc);
    } catch (e) {
      $("#fc-results").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    } finally { busy(btn, false); }
  });

  const PNL = ["Revenue", "COGS", "Gross Margin", "Opex", "Operating Income", "Units (K)", "Headcount"];

  function renderForecast(d) {
    const t = d.table;
    const first = t[0];
    const totalRev = t.reduce((a, r) => a + r.Revenue, 0);
    const totalOi = t.reduce((a, r) => a + r["Operating Income"], 0);
    const growth = d.principle_checks.find((c) => c.metric === "Revenue growth");

    const kpis = `
      <div class="kpis">
        <div class="kpi"><div class="k-label">Next quarter revenue</div><div class="k-val">${money(first.Revenue)}</div>
          <div class="k-sub">${esc(first.quarter)} · ${pct(first["Revenue YoY"])} YoY</div></div>
        <div class="kpi"><div class="k-label">Revenue, ${d.horizon} qtrs</div><div class="k-val">${money(totalRev, 0)}</div>
          <div class="k-sub">${esc(t[0].quarter)} to ${esc(t[t.length - 1].quarter)}</div></div>
        <div class="kpi"><div class="k-label">Operating income, ${d.horizon} qtrs</div><div class="k-val">${money(totalOi, 0)}</div>
          <div class="k-sub">GM and OI calculated from drivers</div></div>
        <div class="kpi ${growth ? (growth.status === "on track" ? "good" : "bad") : ""}"><div class="k-label">${esc(d.principles_fy)} revenue growth</div>
          <div class="k-val">${growth ? pct(growth.forecast) : "–"}</div><div class="k-sub">${growth ? `Target ${pct(growth.target)}` : "Extend horizon to cover the year"}</div></div>
        <div class="kpi"><div class="k-label">Backtest accuracy</div><div class="k-val">${(d.backtest_accuracy * 100).toFixed(1)}%</div>
          <div class="k-sub">1 − MAPE on last 4 quarters held out</div></div>
      </div>`;

    const gp = d.principle_checks.length ? `
      <div class="gp-grid">${d.principle_checks.map((c) => c.status === "info"
        ? `<div class="gp"><div class="g-metric">${esc(c.metric)}</div><div class="g-sub">${esc(c.detail)}</div></div>`
        : `<div class="gp ${c.status === "on track" ? "ok" : "risk"}"><div class="g-metric">${esc(c.metric)}</div>
            <div class="g-vals">${pct(c.forecast, 1, c.metric === "Revenue growth")}</div>
            <div class="g-sub">Target ${pct(c.target, 1, c.metric === "Revenue growth")} · ${c.status === "on track" ? "✓ on track" : "⚠ at risk"} (${(c.gap * 100 >= 0 ? "+" : "") + (c.gap * 100).toFixed(1)} pts)</div></div>`).join("")}</div>`
      : `<p class="muted">No guiding principles found for this selection.</p>`;

    const qs = t.map((r) => r.quarter);
    const pnlTable = `
      <div class="tbl-wrap"><table>
        <thead><tr><th>${d.bu === "Total" ? "Total company" : esc(d.bu)}</th>${qs.map((q) => `<th class="num">${esc(q)}</th>`).join("")}</tr></thead>
        <tbody>
        ${PNL.map((l) => `
          <tr class="${["Gross Margin", "Operating Income"].includes(l) ? "sub" : ""}"><td>${esc(l)}${l.includes("Units") || l === "Headcount" ? "" : " ($M)"}</td>
            ${t.map((r) => `<td class="num">${num(r[l], l === "Headcount" ? 0 : 1)}</td>`).join("")}</tr>
          ${["Revenue", "Operating Income"].includes(l) ? `<tr><td class="muted">&nbsp;&nbsp;QoQ %</td>${t.map((r) => `<td class="num ${cls(r[l + " QoQ"])}">${pct(r[l + " QoQ"])}</td>`).join("")}</tr>
          <tr><td class="muted">&nbsp;&nbsp;YoY %</td>${t.map((r) => `<td class="num ${cls(r[l + " YoY"])}">${pct(r[l + " YoY"])}</td>`).join("")}</tr>` : ""}
          ${l === "Gross Margin" ? `<tr><td class="muted">&nbsp;&nbsp;GM %</td>${t.map((r) => `<td class="num">${pct(r["GM %"], 1, false)}</td>`).join("")}</tr>` : ""}`).join("")}
        </tbody></table></div>`;

    const models = `
      <div class="tbl-wrap"><table>
        <thead><tr><th>Business unit</th><th>Line</th><th>Selected method</th><th class="num">Backtest MAPE</th><th></th></tr></thead>
        <tbody>${d.models.map((m, k) => `
          <tr><td>${esc(m.bu)}</td><td>${esc(m.line)}</td><td class="wrap">${esc(m.method)}</td><td class="num">${(m.backtest_mape * 100).toFixed(1)}%</td>
            <td><details class="models"><summary>All 5 models</summary>
              <table class="lb"><thead><tr><th>Model</th><th class="num">MAPE</th><th class="num">Weight</th></tr></thead>
              <tbody>${m.leaderboard.map((b) => `<tr><td>${esc(b.model)}</td><td class="num">${(b.mape * 100).toFixed(1)}%</td><td class="num">${b.weight ? (b.weight * 100).toFixed(0) + "%" : "–"}</td></tr>`).join("")}</tbody></table>
            </details></td></tr>`).join("")}
        </tbody></table></div>`;

    $("#fc-results").innerHTML = `
      ${kpis}
      <div class="card">
        <div class="card-head"><h2>Forecast</h2>
          <div class="chips" id="line-chips">${PNL.map((l) => `<button class="chip ${l === chartLine ? "on" : ""}" data-l="${esc(l)}">${esc(l)}</button>`).join("")}</div></div>
        <div id="chart"></div>
        <div class="legend"><span><i style="background:var(--blue-dark)"></i>Actuals ${esc(d.source)}</span>
          <span><i style="background:var(--blue)"></i>Forecast</span><span><i style="background:var(--blue-light);height:10px"></i>80% range</span></div>
      </div>
      <div class="card"><div class="card-head"><h2>Guiding principles check</h2><span class="muted">${esc(d.principles_fy)} budget targets for ${d.bu === "Total" ? "the total company" : esc(d.bu)}</span></div>${gp}</div>
      <div class="card"><div class="card-head"><h2>P&amp;L forecast, quarter over quarter</h2></div>${pnlTable}</div>
      <div class="card"><div class="card-head"><h2>Model selection</h2><span class="muted">Each line is backtested on the last 4 quarters. One clear winner is used alone; otherwise the best 3 are averaged, weighted by accuracy.</span></div>${models}</div>`;

    drawChart();
    $$("#line-chips .chip").forEach((c) => c.addEventListener("click", () => {
      chartLine = c.dataset.l;
      $$("#line-chips .chip").forEach((x) => x.classList.toggle("on", x === c));
      drawChart();
    }));
  }

  function drawChart() {
    const s = lastFc.series[chartLine];
    const hq = Object.keys(s.history), fq = Object.keys(s.forecast);
    const all = [...hq, ...fq];
    const W = 860, H = 300, L = 62, R = 16, T = 14, B = 34;
    const vals = [...Object.values(s.history), ...Object.values(s.forecast), ...Object.values(s.low || {}), ...Object.values(s.high || {})];
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.08 || 1;
    lo -= pad; hi += pad;
    const x = (i) => L + (i * (W - L - R)) / (all.length - 1);
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const pts = (keys, obj, off) => keys.map((q, i) => `${x(i + off).toFixed(1)},${y(obj[q]).toFixed(1)}`).join(" ");
    const lastH = hq[hq.length - 1];

    const ticks = 5;
    let grid = "";
    for (let i = 0; i <= ticks; i++) {
      const v = lo + ((hi - lo) * i) / ticks;
      grid += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${v >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(0)}</text>`;
    }
    const xl = all.map((q, i) => (q.endsWith("Q1") || i === all.length - 1) ? `<text x="${x(i)}" y="${H - 12}" text-anchor="middle">${q.replace(" Q1", "")}${q.endsWith("Q1") ? "" : " " + q.split(" ")[1]}</text>` : "").join("");
    let band = "";
    if (s.low) {
      const up = [lastH, ...fq].map((q, i) => `${x(hq.length - 1 + i)},${y(i ? s.high[q] : s.history[q])}`);
      const dn = [lastH, ...fq].map((q, i) => `${x(hq.length - 1 + i)},${y(i ? s.low[q] : s.history[q])}`).reverse();
      band = `<polygon points="${[...up, ...dn].join(" ")}" fill="var(--blue-light)"/>`;
    }
    const fcPts = `${x(hq.length - 1)},${y(s.history[lastH])} ` + pts(fq, s.forecast, hq.length);
    const dots = fq.map((q, i) => `<circle cx="${x(hq.length + i)}" cy="${y(s.forecast[q])}" r="3.5" fill="var(--blue)"><title>${q}: ${num(s.forecast[q])}</title></circle>`).join("");
    const hdots = hq.map((q, i) => `<circle cx="${x(i)}" cy="${y(s.history[q])}" r="2.2" fill="var(--blue-dark)"><title>${q}: ${num(s.history[q])}</title></circle>`).join("");
    const split = x(hq.length - 1);

    $("#chart").innerHTML = `
      <svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(chartLine)} history and forecast">
        <g class="grid">${grid}</g>${xl}
        <line x1="${split}" x2="${split}" y1="${T}" y2="${H - B}" stroke="var(--line)" stroke-dasharray="4 4"/>
        <text x="${split + 6}" y="${T + 10}" style="fill:var(--blue)">forecast →</text>
        ${band}
        <polyline points="${pts(hq, s.history, 0)}" fill="none" stroke="var(--blue-dark)" stroke-width="2"/>
        <polyline points="${fcPts}" fill="none" stroke="var(--blue)" stroke-width="2.5" stroke-dasharray="6 4"/>
        ${hdots}${dots}
      </svg>`;
  }
})();
