// Prompt builder: Excel import. Step 1 uploads the file to
// /api/generator/import/preview (nothing is saved); step 2 shows per-category
// counts with an Add new / Replace all / Skip choice and sends that plan to
// /api/generator/import. The page reloads afterwards to pick up the new items.

(function () {
  const dialog = document.getElementById("import-dialog");
  const form = document.getElementById("import-form");
  const fileInput = document.getElementById("import-file");
  const stepFile = document.getElementById("import-step-file");
  const stepPreview = document.getElementById("import-step-preview");
  const rowsEl = document.getElementById("import-rows");
  const summaryEl = document.getElementById("import-summary");
  const errorEl = document.getElementById("import-error");
  const goBtn = document.getElementById("import-go");

  let preview = null; // response from the preview endpoint

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = !msg;
  }

  function reset() {
    preview = null;
    form.reset();
    rowsEl.innerHTML = "";
    stepFile.hidden = false;
    stepPreview.hidden = true;
    goBtn.textContent = "Preview";
    goBtn.disabled = true;
    showError("");
  }

  async function readJson(res) {
    let data = {};
    try { data = await res.json(); } catch (e) { /* session expired redirect etc. */ }
    if (!res.ok) throw new Error(data.error || (res.redirected ? "Your session expired - reload and log in again." : "Something went wrong (" + res.status + ")"));
    return data;
  }

  function renderPreview() {
    rowsEl.innerHTML = "";
    let total = 0;
    for (const c of preview.categories) {
      total += c.in_file;
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = c.label;
      const inFile = document.createElement("td");
      inFile.textContent = c.in_file;
      const fresh = document.createElement("td");
      fresh.textContent = c.new;
      if (c.already) fresh.title = c.already + " already in Nodebook";
      const actionTd = document.createElement("td");
      const sel = document.createElement("select");
      sel.className = "import-mode";
      sel.dataset.categoryId = c.category_id;
      for (const [value, label] of [["add", "Add new"], ["replace", "Replace all (" + c.current + ")"], ["skip", "Skip"]]) {
        const o = document.createElement("option");
        o.value = value;
        o.textContent = label;
        sel.appendChild(o);
      }
      if (!c.new) sel.value = "skip";
      actionTd.appendChild(sel);
      tr.append(name, inFile, fresh, actionTd);
      rowsEl.appendChild(tr);
    }
    let msg = total + " prompts found across " + preview.categories.length + " categories.";
    if (preview.unmatched.length) msg += " Ignored columns: " + preview.unmatched.join(", ") + ".";
    summaryEl.textContent = msg;
    stepFile.hidden = true;
    stepPreview.hidden = false;
    goBtn.textContent = "Import";
    goBtn.disabled = false;
  }

  async function runPreview() {
    const file = fileInput.files[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    goBtn.disabled = true;
    showError("");
    try {
      const res = await fetch("/api/generator/import/preview", { method: "POST", body, credentials: "same-origin" });
      preview = await readJson(res);
      renderPreview();
    } catch (e) {
      showError(e.message);
      goBtn.disabled = false;
    }
  }

  async function runImport() {
    const plan = [];
    for (const sel of rowsEl.querySelectorAll("select.import-mode")) {
      const cid = Number(sel.dataset.categoryId);
      if (sel.value === "skip") continue;
      const c = preview.categories.find((x) => x.category_id === cid);
      plan.push({ category_id: cid, mode: sel.value, items: c.items });
    }
    if (!plan.length) { showError("Every category is set to Skip."); return; }
    if (plan.some((p) => p.mode === "replace") &&
        !confirm("Replace all deletes the current prompts in those categories. Continue?")) return;
    goBtn.disabled = true;
    showError("");
    try {
      const res = await fetch("/api/generator/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ categories: plan }),
        credentials: "same-origin",
      });
      await readJson(res);
      window.location.reload();
    } catch (e) {
      showError(e.message);
      goBtn.disabled = false;
    }
  }

  document.getElementById("import-btn").addEventListener("click", () => {
    reset();
    dialog.showModal();
  });
  document.getElementById("import-cancel").addEventListener("click", () => dialog.close());
  fileInput.addEventListener("change", () => {
    showError("");
    goBtn.disabled = !fileInput.files.length;
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (preview) runImport(); else runPreview();
  });
})();
