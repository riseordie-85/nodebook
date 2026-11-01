(function () {
	'use strict';

	// Lets this exact same script drive both the AI-gallery and the
	// Reference-images gallery, which are separate collections with
	// separate API endpoints - a page sets window.galleryConfig before
	// loading this file to point it at the right ones; anything it doesn't
	// override falls back to the original AI-gallery endpoints.
	const cfg = Object.assign({
		moveUrl: '/api/images/move',
		deleteUrl: '/api/images/delete',
		favoriteUrlBase: '/api/images/',
		imageRenameUrlBase: null, // only set on pages that support renaming images (Reference)
	}, window.galleryConfig || {});

	const selected = new Set();
	const toolbar = document.getElementById('selection-toolbar');
	const countLabel = document.getElementById('selection-count');

	function updateToolbar() {
		if (selected.size > 0) {
			toolbar.classList.remove('hidden');
			// Just the number in a compact badge, not "N selected" - the
			// full phrase is still there for anyone hovering/reading via
			// the title attribute, it just doesn't eat horizontal space
			// on a narrow screen anymore.
			countLabel.textContent = selected.size;
			countLabel.title = selected.size + ' selected';
		} else {
			toolbar.classList.add('hidden');
		}
	}

	function setCardSelected(card, isSelected) {
		const id = card.dataset.imageId;
		card.classList.toggle('selected', isSelected);
		const checkbox = card.querySelector('.image-select-checkbox');
		if (checkbox) checkbox.checked = isSelected;
		if (isSelected) selected.add(id); else selected.delete(id);
	}

	document.addEventListener('change', (e) => {
		if (!e.target.classList.contains('image-select-checkbox')) return;
		const card = e.target.closest('.image-card');
		setCardSelected(card, e.target.checked);
		updateToolbar();
	});

	document.getElementById('clear-selection-btn')?.addEventListener('click', () => {
		document.querySelectorAll('.image-card.selected').forEach((card) => setCardSelected(card, false));
		updateToolbar();
	});

	// --- Select mode: toggle on to click (or click-and-drag across) thumbnails
	// to select them, instead of navigating to the image's detail page. ---
	let selectModeActive = false;
	let isPainting = false;
	let paintState = true;
	const selectModeBtn = document.getElementById('toggle-select-mode');
	const hasFinePointer = window.matchMedia('(pointer: fine)').matches;

	selectModeBtn?.addEventListener('click', () => {
		selectModeActive = !selectModeActive;
		selectModeBtn.classList.toggle('active', selectModeActive);
		const label = selectModeBtn.querySelector('.icon-btn-label');
		if (label) label.textContent = selectModeActive ? 'Done' : 'Select';
		// Class-based, not a single getElementById: the duplicates page can
		// have several grids (one per group) on the same page at once.
		document.querySelectorAll('.image-grid').forEach((grid) => {
			grid.classList.toggle('select-mode-active', selectModeActive);
		});
	});

	document.addEventListener('mousedown', (e) => {
		if (!selectModeActive) return;
		if (e.target.closest('.image-select-box')) return; // let the checkbox handle its own click normally
		const card = e.target.closest('.image-card');
		if (!card) return;
		e.preventDefault(); // avoid the native drag-ghost/text-selection while painting
		isPainting = true;
		paintState = !card.classList.contains('selected');
		setCardSelected(card, paintState);
		updateToolbar();
	});

	document.addEventListener('mouseover', (e) => {
		if (!isPainting) return;
		if (e.buttons === 0) { isPainting = false; return; } // button released outside the window
		const card = e.target.closest('.image-card');
		if (!card) return;
		setCardSelected(card, paintState);
		updateToolbar();
	});

	document.addEventListener('mouseup', () => {
		isPainting = false;
	});

	document.addEventListener('click', (e) => {
		if (!selectModeActive) return;
		if (e.target.closest('.image-card-link')) {
			// Selection was already applied on mousedown above - this just
			// stops the click from also navigating to the detail page.
			e.preventDefault();
		}
	});

	async function moveImages(imageIds, folderId) {
		const res = await fetch(cfg.moveUrl, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ image_ids: imageIds, folder_id: folderId || null }),
		});
		if (res.ok) {
			window.location.reload();
		} else {
			const data = await res.json().catch(() => ({}));
			alert(data.error || 'Could not move image(s).');
		}
	}

	async function deleteImages(imageIds) {
		const count = imageIds.length;
		const warning = count === 1
			? 'Delete this image? This can\'t be undone.'
			: `Delete these ${count} images? This can't be undone.`;
		if (!confirm(warning)) return;

		const res = await fetch(cfg.deleteUrl, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ image_ids: imageIds }),
		});
		if (res.ok) {
			window.location.reload();
		} else {
			const data = await res.json().catch(() => ({}));
			alert(data.error || 'Could not delete image(s).');
		}
	}

	document.getElementById('move-selected-btn')?.addEventListener('click', () => {
		if (selected.size === 0) return;
		const folderId = document.getElementById('move-target').value;
		moveImages(Array.from(selected), folderId);
	});

	document.getElementById('delete-selected-btn')?.addEventListener('click', () => {
		if (selected.size === 0) return;
		deleteImages(Array.from(selected));
	});

	// --- Favorite toggle ---
	document.addEventListener('click', (e) => {
		const btn = e.target.closest('.favorite-toggle');
		if (!btn) return;
		e.preventDefault();
		e.stopPropagation();
		fetch(`${cfg.favoriteUrlBase}${btn.dataset.imageId}/favorite`, { method: 'POST' })
			.then((res) => res.ok && window.location.reload());
	});

	// --- Image rename (Reference gallery only - cfg.imageRenameUrlBase is
	// null everywhere else, so this silently does nothing there) ---
	document.addEventListener('click', (e) => {
		const btn = e.target.closest('.image-rename-btn');
		if (!btn || !cfg.imageRenameUrlBase) return;
		e.preventDefault();
		e.stopPropagation();
		const currentName = btn.dataset.currentName || '';
		const newName = prompt('Rename to:', currentName);
		if (newName === null || newName.trim() === currentName) return;
		fetch(`${cfg.imageRenameUrlBase}${btn.dataset.imageId}/rename`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ name: newName.trim() }),
		}).then((res) => { if (res.ok) window.location.reload(); });
	});

	// --- Action sheet: Move / Rename / Delete for one image (or the current
	// selection, if the image being acted on is part of one). Opened via a
	// long-press on touch, or a right-click on a fine-pointer (mouse)
	// device - the two platform-native ways to ask for "more options on
	// this thing", now that dragging an image onto a folder is gone. ---
	function openActionSheet(actions) {
		const overlay = document.createElement('div');
		overlay.className = 'quick-move-overlay';
		const menu = document.createElement('div');
		menu.className = 'quick-move-menu';

		actions.forEach((action) => {
			const item = document.createElement('button');
			item.type = 'button';
			item.className = 'quick-move-item' + (action.danger ? ' quick-move-item-danger' : '');
			item.textContent = action.label;
			item.addEventListener('click', () => {
				overlay.remove();
				action.onClick();
			});
			menu.appendChild(item);
		});

		const cancel = document.createElement('button');
		cancel.type = 'button';
		cancel.className = 'quick-move-cancel';
		cancel.textContent = 'Cancel';
		cancel.addEventListener('click', () => overlay.remove());
		menu.appendChild(cancel);

		overlay.appendChild(menu);
		overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
		document.body.appendChild(overlay);
	}

	function openImageActionMenu(card) {
		const moveTarget = document.getElementById('move-target');
		const actions = [];

		if (moveTarget) {
			Array.from(moveTarget.options).forEach((opt) => {
				actions.push({
					label: opt.textContent,
					onClick: () => {
						const id = card.dataset.imageId;
						const idsToMove = selected.has(id) ? Array.from(selected) : [id];
						moveImages(idsToMove, opt.value);
					},
				});
			});
		}

		if (cfg.imageRenameUrlBase) {
			actions.push({
				label: 'Rename',
				onClick: () => {
					const currentName = card.querySelector('.image-rename-btn')?.dataset.currentName || '';
					const newName = prompt('Rename to:', currentName);
					if (newName === null || newName.trim() === currentName) return;
					fetch(`${cfg.imageRenameUrlBase}${card.dataset.imageId}/rename`, {
						method: 'POST',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({ name: newName.trim() }),
					}).then((res) => { if (res.ok) window.location.reload(); });
				},
			});
		}

		actions.push({
			label: 'Delete',
			danger: true,
			onClick: () => {
				const id = card.dataset.imageId;
				const idsToDelete = selected.has(id) ? Array.from(selected) : [id];
				deleteImages(idsToDelete);
			},
		});

		openActionSheet(actions);
	}

	// Touch: long-press
	let longPressTimer = null;
	let longPressCard = null;
	let touchStartX = 0;
	let touchStartY = 0;
	const LONG_PRESS_MS = 500;
	const LONG_PRESS_CANCEL_PX = 12;

	document.addEventListener('touchstart', (e) => {
		if (selectModeActive) return; // tapping already means "toggle selection" there
		const card = e.target.closest('.image-card');
		if (!card) return;
		if (e.target.closest('.image-select-box, .favorite-toggle, .image-rename-btn')) return;
		touchStartX = e.touches[0].clientX;
		touchStartY = e.touches[0].clientY;
		longPressCard = card;
		longPressTimer = setTimeout(() => {
			if (longPressCard) {
				if (navigator.vibrate) navigator.vibrate(20);
				openImageActionMenu(longPressCard);
			}
			longPressTimer = null;
		}, LONG_PRESS_MS);
	}, { passive: true });

	document.addEventListener('touchmove', (e) => {
		if (!longPressTimer) return;
		const dx = Math.abs(e.touches[0].clientX - touchStartX);
		const dy = Math.abs(e.touches[0].clientY - touchStartY);
		if (dx > LONG_PRESS_CANCEL_PX || dy > LONG_PRESS_CANCEL_PX) {
			clearTimeout(longPressTimer);
			longPressTimer = null;
			longPressCard = null;
		}
	}, { passive: true });

	document.addEventListener('touchend', () => {
		if (longPressTimer) {
			clearTimeout(longPressTimer);
			longPressTimer = null;
		}
		longPressCard = null;
	});

	// Desktop: right-click. On touch this event also fires natively after a
	// long-press, so it's kept to just suppressing the browser's own menu
	// there (our touchstart timer above already opened the sheet) - only a
	// fine pointer gets it opening the sheet directly, since a mouse has no
	// long-press gesture of its own to do that job.
	document.addEventListener('contextmenu', (e) => {
		const card = e.target.closest('.image-card');
		if (!card) return;
		e.preventDefault();
		if (hasFinePointer) {
			openImageActionMenu(card);
		}
	});

	// --- Grid density: 1/2/3 columns, remembered per-device. Replaces the
	// old grid/list view toggle. Labeled by thumbnail SIZE (Large/Medium/
	// Small), not column count, since on a wide desktop screen the same
	// setting yields many more than 1/2/3 actual columns - see the CSS.
	const densityBtn = document.getElementById('grid-density-toggle');
	const DENSITY_KEY = 'nodebook-grid-cols';
	const DENSITY_LABELS = { 1: 'Large', 2: 'Medium', 3: 'Small' };

	function applyDensity(cols) {
		document.querySelectorAll('.image-grid').forEach((g) => {
			g.classList.remove('cols-1', 'cols-2', 'cols-3');
			g.classList.add(`cols-${cols}`);
		});
		if (densityBtn) {
			const glyph = densityBtn.querySelector('.icon-glyph');
			const label = densityBtn.querySelector('.icon-btn-label');
			if (glyph) glyph.textContent = '\u25aa'.repeat(cols);
			if (label) label.textContent = DENSITY_LABELS[cols];
		}
	}

	let currentCols = parseInt(localStorage.getItem(DENSITY_KEY), 10);
	if (![1, 2, 3].includes(currentCols)) currentCols = 2;
	applyDensity(currentCols);

	densityBtn?.addEventListener('click', () => {
		currentCols = currentCols >= 3 ? 1 : currentCols + 1;
		localStorage.setItem(DENSITY_KEY, String(currentCols));
		applyDensity(currentCols);
	});

	// --- Sort toggle: newest/oldest by save date. A straight two-state
	// flip rather than the old 4-option dropdown - upload-date sorting
	// still works server-side if ever linked to directly, it's just not in
	// this control anymore. Initial state comes from the server-rendered
	// data-sort attribute (the actual current sort_key), not a re-parse of
	// the URL, so it can never drift from what the page actually shows. ---
	document.getElementById('sort-toggle')?.addEventListener('click', function () {
		const current = this.dataset.sort || 'new_saved';
		const next = current === 'old_saved' ? 'new_saved' : 'old_saved';
		const params = new URLSearchParams(window.location.search);
		params.set('sort', next);
		params.delete('page');
		window.location.search = params.toString();
	});

	// --- Folder navigation dropdown. Preserves whatever sort is currently
	// active (only the folder param changes), and always resets to page 1
	// since "page 2" under one folder doesn't mean anything in another. ---
	document.getElementById('folder-select')?.addEventListener('change', function () {
		const params = new URLSearchParams(window.location.search);
		if (this.value) {
			params.set('folder', this.value);
		} else {
			params.delete('folder');
		}
		params.delete('page');
		window.location.search = params.toString();
	});

	// Pages like Duplicates can pre-check some boxes server-side (suggested
	// deletions) - the HTML `checked` attribute alone doesn't fire a 'change'
	// event, so without this the toolbar/selection state would silently be
	// out of sync with what's visually checked when the page first loads.
	document.querySelectorAll('.image-select-checkbox:checked').forEach((checkbox) => {
		const card = checkbox.closest('.image-card');
		if (card) setCardSelected(card, true);
	});
	updateToolbar();
})();
