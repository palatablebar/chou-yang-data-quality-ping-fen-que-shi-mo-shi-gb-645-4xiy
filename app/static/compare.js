/* 批次对比：提交两个 run_id，成功后跳到该对比记录的只读快照页。 */
(function () {
  "use strict";

  function status(text, kind) {
    const node = document.getElementById("compare-status");
    if (!node) return;
    node.textContent = text;
    node.className = "status" + (kind ? " status--" + kind : "");
  }

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const message = payload.message || payload.detail || response.statusText;
      throw new Error(message + "（HTTP " + response.status + "）");
    }
    return payload;
  }

  function bind() {
    const form = document.getElementById("compare-form");
    if (!form) return;
    const options = Array.prototype.slice.call(form.querySelectorAll("option"));
    if (options.length >= 2) {
      const target = document.getElementById("target-run");
      target.selectedIndex = 1;
    }
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const data = new FormData(form);
      const base = String(data.get("base_run_id") || "");
      const target = String(data.get("target_run_id") || "");
      if (base === target) {
        status("请选择两条不同的运行记录。", "bad");
        return;
      }
      const button = document.getElementById("compare-btn");
      button.disabled = true;
      status("对比计算中…");
      try {
        const payload = await postJSON("/api/comparisons", {
          base_run_id: base,
          target_run_id: target
        });
        status((payload.replay ? "命中已有对比记录，" : "对比完成，") + "正在刷新…", "good");
        window.location.assign("/compare?comparison_id="
          + encodeURIComponent(payload.comparison_id));
      } catch (error) {
        status(error.message, "bad");
        button.disabled = false;
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
