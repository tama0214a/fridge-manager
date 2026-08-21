/* 入庫・編集フォーム共通: 保管期限のクイック設定と「期限なし」切り替え */

function fmtDate(d) {
  const pad = function (n) { return String(n).padStart(2, "0"); };
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}

/* 入庫日を起点に days 日 / months ヶ月後を保管期限にセットする */
function addPeriod(days, months) {
  const stored = document.getElementById("stored_date").value;
  if (!stored) { return; }
  const p = stored.split("-").map(Number);
  const d = new Date(p[0], p[1] - 1, p[2]);
  if (months) { d.setMonth(d.getMonth() + months); }
  if (days) { d.setDate(d.getDate() + days); }
  const noExp = document.getElementById("no_expiry");
  if (noExp && noExp.checked) {
    noExp.checked = false;
    toggleExpiry();
  }
  document.getElementById("expiry_date").value = fmtDate(d);
}

function toggleExpiry() {
  const noExp = document.getElementById("no_expiry");
  const field = document.getElementById("expiry_date");
  if (!noExp || !field) { return; }
  field.disabled = noExp.checked;
  document.querySelectorAll(".chip").forEach(function (c) {
    c.disabled = noExp.checked;
  });
}

document.addEventListener("DOMContentLoaded", toggleExpiry);
