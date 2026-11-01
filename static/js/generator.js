// Prompt builder: one dropdown per category, with add / edit / remove of the
// items in each. Items live in the database (see /api/generator/items...);
// the current selection per category is remembered per browser.

(function () {
  const STORAGE_KEY = "nodebook-builder-selection";
  const cats = JSON.parse(document.getElementById("builder-data").textContent);
  const cardsEl = document.getElementById("builder-cards");
  const outputEl = document.getElementById("built-prompt");
  const dialog = document.getElementById("item-dialog");
  const dialogTitle = document.getElementById("item-dialog-title");
  const itemText = document.getElementById("item-text");
  const itemError = document.getElementById("item-error");
  const saveBtn = document.getElementById("item-save");

  const selected = {}; // category id -> item id (or null when the category is empty)
  let dialogMode = null; // {type: "add"|"edit", cat, item?}

  // ---------- helpers ----------
  function loadSelection() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; } catch (e) { return {}; }
  }
  function saveSelection() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(selected)); } catch (e) { /* private mode etc. */ }
  }
  function randomItem(cat, avoidId) {
    if (!cat.items.length) return null;
    const pool = cat.items.length > 1 ? cat.items.filter((i) => i.id !== avoidId) : cat.items;
    return pool[Math.floor(Math.random() * pool.length)].id;
  }
  function itemById(cat, id) { return cat.items.find((i) => i.id === id) || null; }

  async function api(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON (e.g. session expired redirect) */ }
    if (!res.ok) throw new Error(data.error || (res.redirected ? "Your session expired - reload and log in again." : "Something went wrong (" + res.status + ")"));
    return data;
  }

  function sentence(label, text) {
    return label + ": " + text + (/[.!?]$/.test(text) ? "" : ".");
  }

  // ---------- rendering ----------
  function buildPrompt() {
    const parts = [];
    for (const cat of cats) {
      const item = itemById(cat, selected[cat.id]);
      if (item) parts.push(sentence(cat.label, item.text));
    }
    outputEl.value = parts.join("\n\n");
  }

  function renderCard(cat) {
    const card = cardsEl.querySelector('[data-cat="' + cat.id + '"]');
    const select = card.querySelector("select");
    const preview = card.querySelector(".builder-preview");
    const empty = cat.items.length === 0;

    select.innerHTML = "";
    if (empty) {
      const o = document.createElement("option");
      o.textContent = "(empty - add an item)";
      select.appendChild(o);
    } else {
      for (const item of cat.items) {
        const o = document.createElement("option");
        o.value = item.id;
        o.textContent = item.text;
        select.appendChild(o);
      }
      select.value = selected[cat.id];
    }
    select.disabled = empty;
    card.querySelector(".builder-count").textContent = cat.items.length + (cat.items.length === 1 ? " item" : " items");
    const current = itemById(cat, selected[cat.id]);
    preview.textContent = current ? current.text : "";
    for (const btn of card.querySelectorAll('[data-action="random"],[data-action="edit"],[data-action="remove"]')) {
      btn.disabled = empty;
    }
  }

  function buildCards() {
    for (const cat of cats) {
      const card = document.createElement("section");
      card.className = "builder-card";
      card.dataset.cat = cat.id;
      card.innerHTML =
        '<div class="builder-card-head">' +
          '<label class="builder-label" for="sel-' + cat.id + '"></label>' +
          '<span class="builder-count muted"></span>' +
        '</div>' +
        '<select class="builder-select" id="sel-' + cat.id + '"></select>' +
        '<p class="builder-preview"></p>' +
        '<div class="builder-actions">' +
          '<button type="button" class="btn-icon" data-action="random" title="Randomize this category" aria-label="Randomize">&#127922;</button>' +
          '<button type="button" class="btn-icon" data-action="add" title="Add an item" aria-label="Add item">+</button>' +
          '<button type="button" class="btn-icon" data-action="edit" title="Edit the selected item" aria-label="Edit item">&#9998;</button>' +
          '<button type="button" class="btn-icon btn-icon-danger" data-action="remove" title="Remove the selected item" aria-label="Remove item">&times;</button>' +
        '</div>';
      card.querySelector(".builder-label").textContent = cat.label;
      cardsEl.appendChild(card);
      renderCard(cat);
    }
  }

  function refresh(cat) {
    if (cat) renderCard(cat);
    saveSelection();
    buildPrompt();
  }

  // ---------- add / edit dialog ----------
  function openDialog(mode) {
    dialogMode = mode;
    dialogTitle.textContent = (mode.type === "add" ? "Add to " : "Edit in ") + mode.cat.label;
    itemText.value = mode.type === "edit" ? mode.item.text : "";
    itemError.hidden = true;
    saveBtn.disabled = false;
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    itemText.focus();
  }
  function closeDialog() {
    if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
    dialogMode = null;
  }

  async function submitDialog(e) {
    e.preventDefault();
    if (!dialogMode) return;
    const text = itemText.value.trim();
    if (!text) { itemError.textContent = "Enter some text first."; itemError.hidden = false; return; }
    saveBtn.disabled = true;
    try {
      const { cat, type, item } = dialogMode;
      if (type === "add") {
        const created = await api("/api/generator/items", { category_id: cat.id, text });
        cat.items.push({ id: created.id, text: created.text });
        selected[cat.id] = created.id;
      } else {
        const updated = await api("/api/generator/items/" + item.id + "/update", { text });
        item.text = updated.text;
      }
      closeDialog();
      refresh(cat);
    } catch (err) {
      itemError.textContent = err.message;
      itemError.hidden = false;
      saveBtn.disabled = false;
    }
  }

  async function removeSelected(cat) {
    const item = itemById(cat, selected[cat.id]);
    if (!item) return;
    const preview = item.text.length > 90 ? item.text.slice(0, 90) + "..." : item.text;
    if (!confirm('Remove from ' + cat.label + '?\n\n"' + preview + '"')) return;
    try {
      await api("/api/generator/items/" + item.id + "/delete");
    } catch (err) { alert(err.message); return; }
    const idx = cat.items.findIndex((i) => i.id === item.id);
    cat.items.splice(idx, 1);
    selected[cat.id] = cat.items.length ? cat.items[Math.min(idx, cat.items.length - 1)].id : null;
    refresh(cat);
  }

  // ---------- copy ----------
  async function copyText(text, btn) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {
      outputEl.select();
      try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
    }
    const original = btn.dataset.label || btn.textContent;
    btn.dataset.label = original;
    btn.textContent = ok ? "Copied!" : "Copy failed";
    setTimeout(() => { btn.textContent = original; }, 1500);
  }

  // ---------- wiring ----------
  const stored = loadSelection();
  for (const cat of cats) {
    const keep = stored[cat.id];
    selected[cat.id] = itemById(cat, keep) ? keep : (cat.items[0] ? cat.items[0].id : null);
  }
  buildCards();
  buildPrompt();

  cardsEl.addEventListener("change", (e) => {
    if (!e.target.matches("select")) return;
    const cat = cats.find((c) => c.id === Number(e.target.closest(".builder-card").dataset.cat));
    selected[cat.id] = Number(e.target.value);
    refresh(cat);
  });

  cardsEl.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const cat = cats.find((c) => c.id === Number(btn.closest(".builder-card").dataset.cat));
    switch (btn.dataset.action) {
      case "random": selected[cat.id] = randomItem(cat, selected[cat.id]); refresh(cat); break;
      case "add": openDialog({ type: "add", cat }); break;
      case "edit": openDialog({ type: "edit", cat, item: itemById(cat, selected[cat.id]) }); break;
      case "remove": removeSelected(cat); break;
    }
  });

  document.getElementById("randomize-all-btn").addEventListener("click", () => {
    for (const cat of cats) { selected[cat.id] = randomItem(cat, selected[cat.id]); renderCard(cat); }
    saveSelection();
    buildPrompt();
  });
  document.getElementById("copy-top-btn").addEventListener("click", (e) => copyText(outputEl.value, e.currentTarget));
  document.getElementById("copy-full-btn").addEventListener("click", (e) => copyText(outputEl.value, e.currentTarget));

  document.getElementById("item-form").addEventListener("submit", submitDialog);
  document.getElementById("item-cancel").addEventListener("click", closeDialog);
  dialog.addEventListener("cancel", () => { dialogMode = null; });
  // Ctrl/Cmd+Enter saves from inside the textarea.
  itemText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitDialog(e);
  });
})();
