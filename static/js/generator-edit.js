// Bulk editor for the prompt builder: one category at a time, every prompt
// editable in place (saved when you click away), tick-and-delete for many,
// and paste-a-list to add many. Talks to the same /api/generator/items/...
// endpoints as the builder, plus bulk-add and bulk-delete.

(function () {
  const cats = JSON.parse(document.getElementById("edit-data").textContent);
  const tabsEl = document.getElementById("edit-tabs");
  const listEl = document.getElementById("edit-list");
  const emptyEl = document.getElementById("edit-empty");
  const searchEl = document.getElementById("edit-search");
  const selectAllEl = document.getElementById("edit-select-all");
  const showingEl = document.getElementById("edit-showing");
  const deleteBtn = document.getElementById("edit-delete-btn");
  const addDetails = document.getElementById("edit-add");
  const addText = document.getElementById("edit-add-text");
  const addBtn = document.getElementById("edit-add-btn");
  const addNote = document.getElementById("edit-add-note");
  const addCatEl = document.getElementById("edit-add-cat");

  let current = cats[0];
  const selected = new Set(); // item ids ticked in the current category

  // ---------- helpers ----------
  async function api(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON, e.g. a login redirect */ }
    if (!res.ok) throw new Error(data.error || (res.redirected ? "Your session expired - reload and log in again." : "Something went wrong (" + res.status + ")"));
    return data;
  }

  function autosize(ta) {
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + 2 + "px";
  }

  function visibleItems() {
    const q = searchEl.value.trim().toLowerCase();
    return q ? current.items.filter((i) => i.text.toLowerCase().includes(q)) : current.items;
  }

  // ---------- rendering ----------
  function renderTabs() {
    tabsEl.innerHTML = "";
    for (const cat of cats) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "edit-tab" + (cat === current ? " active" : "");
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", cat === current ? "true" : "false");
      b.dataset.cat = cat.id;
      b.textContent = cat.label + " ";
      const n = document.createElement("span");
      n.className = "edit-tab-count";
      n.textContent = cat.items.length;
      b.appendChild(n);
      tabsEl.appendChild(b);
    }
  }

  function renderRow(item) {
    const li = document.createElement("li");
    li.className = "edit-row";
    li.dataset.id = item.id;

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "edit-check";
    cb.checked = selected.has(item.id);
    cb.setAttribute("aria-label", "Select this prompt");

    const main = document.createElement("div");
    main.className = "edit-main";
    const ta = document.createElement("textarea");
    ta.className = "edit-text";
    ta.rows = 1;
    ta.value = item.text;
    ta.dataset.saved = item.text;
    const status = document.createElement("div");
    status.className = "edit-status";
    status.hidden = true;
    main.append(ta, status);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn-icon btn-icon-danger edit-del";
    del.title = "Delete this prompt";
    del.setAttribute("aria-label", "Delete this prompt");
    del.innerHTML = "&times;";

    li.append(cb, main, del);
    return li;
  }

  function renderList() {
    const items = visibleItems();
    listEl.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const item of items) frag.appendChild(renderRow(item));
    listEl.appendChild(frag);
    for (const ta of listEl.querySelectorAll("textarea")) autosize(ta);

    const filtered = searchEl.value.trim() !== "";
    emptyEl.hidden = items.length > 0;
    emptyEl.textContent = filtered ? "No prompts match that filter."
      : "This category has no prompts yet. Use Add prompts above.";
    showingEl.textContent = filtered
      ? items.length + " of " + current.items.length + " shown"
      : current.items.length + (current.items.length === 1 ? " prompt" : " prompts");
    addCatEl.textContent = current.label;
    syncSelectionUi();
  }

  function syncSelectionUi() {
    // Only ids that still exist in this category count.
    const ids = new Set(current.items.map((i) => i.id));
    for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
    deleteBtn.disabled = selected.size === 0;
    deleteBtn.textContent = selected.size ? "Delete selected (" + selected.size + ")" : "Delete selected";
    const shown = visibleItems();
    const shownSelected = shown.filter((i) => selected.has(i.id)).length;
    selectAllEl.checked = shown.length > 0 && shownSelected === shown.length;
    selectAllEl.indeterminate = shownSelected > 0 && shownSelected < shown.length;
    selectAllEl.disabled = shown.length === 0;
  }

  function switchTo(cat) {
    current = cat;
    selected.clear();
    searchEl.value = "";
    renderTabs();
    renderList();
    try { history.replaceState(null, "", "#" + cat.key); } catch (e) { /* ignore */ }
  }

  function setStatus(li, msg, kind) {
    const el = li.querySelector(".edit-status");
    el.textContent = msg || "";
    el.hidden = !msg;
    el.className = "edit-status" + (kind ? " " + kind : "");
    li.classList.toggle("has-error", kind === "error");
  }

  // ---------- saving an edited row ----------
  async function saveRow(ta) {
    const li = ta.closest(".edit-row");
    const id = Number(li.dataset.id);
    const item = current.items.find((i) => i.id === id);
    if (!item) return;
    const text = ta.value.trim();
    if (text === ta.dataset.saved) { ta.value = ta.dataset.saved; autosize(ta); setStatus(li, ""); return; }
    if (!text) {
      ta.value = ta.dataset.saved;
      autosize(ta);
      setStatus(li, "A prompt can't be empty - use × to delete it.", "error");
      return;
    }
    setStatus(li, "Saving...", "");
    try {
      const res = await api("/api/generator/items/" + id + "/update", { text });
      item.text = res.text;
      ta.value = res.text;
      ta.dataset.saved = res.text;
      autosize(ta);
      setStatus(li, "Saved", "ok");
      setTimeout(() => { if (li.querySelector(".edit-status").textContent === "Saved") setStatus(li, ""); }, 1500);
    } catch (err) {
      setStatus(li, err.message, "error"); // keep their edit so they can fix it
    }
  }

  // ---------- deleting ----------
  async function deleteIds(ids, what) {
    if (!ids.length) return;
    if (!confirm("Delete " + what + "? This can't be undone.")) return;
    try {
      await api("/api/generator/items/bulk-delete", { ids });
    } catch (err) { alert(err.message); return; }
    const gone = new Set(ids);
    current.items = current.items.filter((i) => !gone.has(i.id));
    cats.find((c) => c.id === current.id).items = current.items;
    for (const id of ids) selected.delete(id);
    renderTabs();
    renderList();
  }

  // ---------- adding many ----------
  async function addMany() {
    const text = addText.value;
    if (!text.trim()) { addNote.textContent = "Paste at least one prompt."; return; }
    addBtn.disabled = true;
    addNote.textContent = "";
    try {
      const res = await api("/api/generator/items/bulk-add", { category_id: current.id, text });
      current.items.push(...res.added);
      addText.value = "";
      addNote.textContent = "Added " + res.added.length + (res.skipped ? ", skipped " + res.skipped + " (blank or already there)" : "") + ".";
      renderTabs();
      renderList();
    } catch (err) {
      addNote.textContent = err.message;
    } finally {
      addBtn.disabled = false;
    }
  }

  // ---------- wiring ----------
  tabsEl.addEventListener("click", (e) => {
    const b = e.target.closest(".edit-tab");
    if (!b) return;
    const cat = cats.find((c) => c.id === Number(b.dataset.cat));
    if (cat && cat !== current) { addNote.textContent = ""; switchTo(cat); }
  });

  searchEl.addEventListener("input", renderList);

  listEl.addEventListener("input", (e) => { if (e.target.matches("textarea")) autosize(e.target); });
  listEl.addEventListener("change", (e) => {
    if (e.target.matches("textarea")) { saveRow(e.target); return; }
    if (e.target.matches(".edit-check")) {
      const id = Number(e.target.closest(".edit-row").dataset.id);
      if (e.target.checked) selected.add(id); else selected.delete(id);
      syncSelectionUi();
    }
  });
  listEl.addEventListener("keydown", (e) => {
    if (!e.target.matches("textarea")) return;
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.target.blur(); }  // Enter saves; Shift+Enter would add a line break but prompts are single-line
    if (e.key === "Escape") { e.target.value = e.target.dataset.saved; autosize(e.target); e.target.blur(); }
  });
  listEl.addEventListener("click", (e) => {
    const del = e.target.closest(".edit-del");
    if (!del) return;
    const li = del.closest(".edit-row");
    const id = Number(li.dataset.id);
    const item = current.items.find((i) => i.id === id);
    const preview = item && item.text.length > 80 ? item.text.slice(0, 80) + "..." : (item ? item.text : "");
    deleteIds([id], '"' + preview + '"');
  });

  selectAllEl.addEventListener("change", () => {
    for (const item of visibleItems()) {
      if (selectAllEl.checked) selected.add(item.id); else selected.delete(item.id);
    }
    for (const cb of listEl.querySelectorAll(".edit-check")) cb.checked = selectAllEl.checked;
    syncSelectionUi();
  });

  deleteBtn.addEventListener("click", () => {
    const ids = [...selected];
    deleteIds(ids, ids.length === 1 ? "this prompt" : ids.length + " prompts from " + current.label);
  });

  window.addEventListener("resize", () => { for (const ta of listEl.querySelectorAll("textarea")) autosize(ta); });

  addBtn.addEventListener("click", addMany);
  addText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); addMany(); }
  });

  // Start on the category named in the URL (#camera), else the first.
  const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
  current = cats.find((c) => c.key === hash) || cats[0];
  renderTabs();
  renderList();
})();
