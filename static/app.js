"use strict";
const $ = (id) => document.getElementById(id);
const yen = (v) => "¥" + Number(v).toLocaleString("ja-JP");
const labels = {
  dashboard: "売上ダッシュボード",
  imports: "CSV取り込み",
  reports: "営業日報",
  history: "処理履歴",
};
const statuses = {
  draft: "下書き",
  confirmed: "確定済み",
  stale: "再集計が必要",
};
let currentReport = null;
let query = "";
let dirty = false;
function ask(message, acceptLabel) {
  return new Promise((resolve) => {
    const dialog = $("confirmation");
    $("confirmation-message").textContent = message;
    $("confirmation-accept").textContent = acceptLabel;
    const finish = (answer) => {
      dialog.close();
      dialog.oncancel = null;
      resolve(answer);
    };
    $("confirmation-cancel").onclick = () => finish(false);
    $("confirmation-accept").onclick = () => finish(true);
    dialog.oncancel = (e) => {
      e.preventDefault();
      finish(false);
    };
    dialog.showModal();
  });
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notify(message, error = false) {
  $("notice").hidden = false;
  $("notice").className = error ? "error" : "";
  $("notice").textContent = message;
}
async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(
      Array.isArray(detail)
        ? detail.map((d) => (typeof d === "string" ? d : d.msg)).join("\n")
        : detail || "処理に失敗しました。",
    );
  }
  return data;
}
async function guarded(button, task) {
  button.disabled = true;
  try {
    await task();
  } catch (e) {
    notify(e.message || "接続できませんでした。", true);
  } finally {
    button.disabled = false;
    if (currentReport) updateButtons();
  }
}
function navigate(view) {
  document.querySelectorAll(".view").forEach((s) => (s.hidden = s.id !== view));
  document
    .querySelectorAll("[data-view]")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("breadcrumb").textContent = labels[view];
  if (view === "history") loadHistory().catch((e) => notify(e.message, true));
  if (view === "reports") loadReports().catch((e) => notify(e.message, true));
}
document
  .querySelectorAll("[data-view],[data-go]")
  .forEach((b) =>
    b.addEventListener("click", () => navigate(b.dataset.view || b.dataset.go)),
  );

async function loadDashboard() {
  const data = await api("/api/summary" + query);
  $("revenue").textContent = yen(data.total.revenue);
  $("quantity").textContent = data.total.quantity.toLocaleString("ja-JP");
  $("day-count").textContent = data.days.length;
  $("daily-chart").replaceChildren();
  const max = Math.max(1, ...data.days.map((d) => d.revenue));
  data.days.forEach((d) => {
    const row = el("div", undefined, "chart-row");
    row.append(el("span", d.sold_on.slice(5).replace("-", "/")));
    const track = el("div", undefined, "chart-track");
    const meter = el("meter");
    meter.min = 0;
    meter.max = max;
    meter.value = d.revenue;
    meter.setAttribute("aria-label", `${d.sold_on} 売上 ${d.revenue}円`);
    track.append(meter);
    row.append(track, el("strong", yen(d.revenue)));
    $("daily-chart").append(row);
  });
  if (!data.days.length)
    $("daily-chart").append(
      el(
        "p",
        query
          ? "条件に一致する売上はありません。条件を変更してください。"
          : "データがありません。CSV取り込みから始めましょう。",
        "empty",
      ),
    );
  $("products").replaceChildren();
  data.products.forEach((p) => {
    const row = el("tr");
    row.append(
      el("td", p.product),
      el("td", p.quantity.toLocaleString("ja-JP"), "number"),
      el("td", yen(p.revenue), "number"),
    );
    $("products").append(row);
  });
  if (!data.products.length) {
    const row = el("tr");
    const cell = el(
      "td",
      query
        ? "条件に一致する商品はありません。"
        : "取り込み後に商品別売上を表示します。",
      "empty",
    );
    cell.colSpan = 3;
    row.append(cell);
    $("products").append(row);
  }
  $("export-products").href = "/api/exports/products.csv" + query;
  const active = new URLSearchParams(query);
  $("filter-summary").textContent = query
    ? `集計中：${active.get("start") || "開始日なし"} 〜 ${active.get("end") || "終了日なし"} / 商品名：${active.get("product") || "すべて"}（部分一致・大文字小文字を区別）`
    : "すべての商品・期間を集計しています。";
}
$("filter-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("start").value && $("end").value && $("start").value > $("end").value)
    return notify("開始日は終了日以前にしてください。", true);
  const params = new URLSearchParams();
  if ($("start").value) params.set("start", $("start").value);
  if ($("end").value) params.set("end", $("end").value);
  if ($("product-filter").value.trim())
    params.set("product", $("product-filter").value.trim());
  query = params.size ? "?" + params.toString() : "";
  guarded(e.submitter, loadDashboard);
});
$("reset-filter").addEventListener("click", () => {
  $("start").value = "";
  $("end").value = "";
  $("product-filter").value = "";
  query = "";
  guarded($("reset-filter"), loadDashboard);
});

async function doImport(file) {
  if (!(await discardChanges())) return;
  if (file.size > 2 * 1024 * 1024) throw new Error("ファイルの上限は2MBです。");
  const form = new FormData();
  form.append("file", file, file.name || "sample-sales.csv");
  const result = await api("/api/imports", { method: "POST", body: form });
  $("import-result").textContent =
    `${result.row_count}行を取り込みました。ダッシュボードで確認できます。`;
  notify(`${result.row_count}行の売上を取り込みました。`);
  // Clear potentially obsolete report data after an import.
  currentReport = null;
  dirty = false;
  $("report-content").value = "";
  $("report-content").disabled = true;
  $("report-title").textContent = "日報プレビュー";
  $("report-status").textContent = "未選択";
  ["save-report", "confirm-report", "download-report"].forEach(
    (id) => ($(id).disabled = true),
  );
  await loadDashboard();
}
$("upload-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const file = $("csv-file").files[0];
  if (file) guarded(e.submitter, () => doImport(file));
});
$("sample-import").addEventListener("click", () =>
  guarded($("sample-import"), async () => {
    const response = await fetch("/api/sample");
    if (!response.ok) throw new Error("サンプルを取得できませんでした。");
    await doImport(
      new File([await response.blob()], "sample-sales.csv", {
        type: "text/csv",
      }),
    );
  }),
);

async function loadReports() {
  const [data, reports] = await Promise.all([
    api("/api/summary"),
    api("/api/reports"),
  ]);
  const previousDate = $("report-date").value;
  $("report-date").replaceChildren();
  [...data.days].reverse().forEach((d) => {
    const option = el("option", d.sold_on);
    option.value = d.sold_on;
    $("report-date").append(option);
  });
  if (data.days.some((d) => d.sold_on === previousDate))
    $("report-date").value = previousDate;
  $("draft-form").querySelector("button").disabled = !data.days.length;
  $("report-list").replaceChildren();
  reports.forEach((r) => {
    const button = el("button", undefined, "report-item");
    button.append(el("span", r.sold_on), el("span", statuses[r.status], "tag"));
    button.addEventListener("click", async () => {
      if (!(await discardChanges())) return;
      $("report-date").value = r.sold_on;
      guarded(button, () => openReport(r.sold_on));
    });
    $("report-list").append(button);
  });
  if (!reports.length)
    $("report-list").append(el("p", "日報はまだありません。", "muted"));
}
async function discardChanges() {
  return (
    !dirty ||
    (await ask("保存していない日報の変更を破棄しますか？", "変更を破棄する"))
  );
}
function updateButtons() {
  const editable = currentReport && currentReport.status === "draft";
  $("save-report").disabled = !editable;
  $("confirm-report").disabled = !editable;
  $("download-report").disabled =
    !currentReport || currentReport.status !== "confirmed";
}
function renderReport(r) {
  currentReport = r;
  dirty = false;
  $("report-title").textContent = r.sold_on + " 営業日報";
  $("report-status").textContent = statuses[r.status];
  $("report-content").value = r.content;
  $("report-content").disabled = r.status !== "draft";
  updateButtons();
}
async function openReport(day) {
  renderReport(await api(`/api/reports/${day}/draft`, { method: "POST" }));
  await loadReports();
}
$("draft-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await discardChanges())
    guarded(e.submitter, () => openReport($("report-date").value));
});
$("report-content").addEventListener("input", () => {
  dirty = true;
});
window.addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
async function persistReport(confirm) {
  const r = currentReport;
  const saved = await api(
    `/api/reports/${r.sold_on}${confirm ? "/confirm" : ""}`,
    {
      method: confirm ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: $("report-content").value,
        version: r.version,
      }),
    },
  );
  renderReport(saved);
  await loadReports();
  notify(
    confirm
      ? "日報を確定しました。JSONで保存できます。"
      : "下書きを保存しました。",
  );
}
$("save-report").addEventListener("click", () =>
  guarded($("save-report"), () => persistReport(false)),
);
$("confirm-report").addEventListener("click", async () => {
  if (
    await ask(
      "日報を確定しますか？ 確定後は編集と同日の売上追加ができません。",
      "確定する",
    )
  )
    guarded($("confirm-report"), () => persistReport(true));
});
$("download-report").addEventListener("click", () =>
  guarded($("download-report"), async () => {
    const payload = await api(`/api/reports/${currentReport.sold_on}/export`);
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(payload, null, 2)], {
        type: "application/json",
      }),
    );
    const link = el("a");
    link.href = url;
    link.download = `report-${currentReport.sold_on}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }),
);
async function loadHistory() {
  const data = await api("/api/imports");
  $("history-list").replaceChildren();
  data.forEach((r) => {
    const row = el("div", undefined, "history-item");
    row.append(
      el(
        "span",
        r.status === "success" ? "取り込み成功" : "取り込み失敗",
        "history-status " + r.status,
      ),
    );
    const content = el("div");
    content.append(
      el("h3", r.filename),
      el(
        "p",
        `${new Date(r.created_at).toLocaleString("ja-JP")} / ${r.row_count}行`,
      ),
      el("p", r.message),
    );
    row.append(content);
    $("history-list").append(row);
  });
  if (!data.length)
    $("history-list").append(
      el("p", "取り込み履歴はまだありません。", "empty"),
    );
}
$("refresh-history").addEventListener("click", () =>
  guarded($("refresh-history"), loadHistory),
);
loadDashboard().catch((e) => notify(e.message, true));
