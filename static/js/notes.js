(function () {
	'use strict';

	// --- List page: pin/duplicate/delete buttons on each note card ---
	document.querySelectorAll('.note-pin-btn').forEach((btn) => {
		btn.addEventListener('click', (e) => {
			e.preventDefault();
			fetch(`/api/notes/${btn.dataset.noteId}/pin`, { method: 'POST' })
				.then((res) => { if (res.ok) window.location.reload(); });
		});
	});

	document.querySelectorAll('.note-duplicate-btn').forEach((btn) => {
		btn.addEventListener('click', (e) => {
			e.preventDefault();
			fetch(`/api/notes/${btn.dataset.noteId}/duplicate`, { method: 'POST' })
				.then((res) => res.json())
				.then((data) => { if (data.id) window.location.href = `/notes/${data.id}`; });
		});
	});

	document.querySelectorAll('.note-delete-btn').forEach((btn) => {
		btn.addEventListener('click', (e) => {
			e.preventDefault();
			if (!confirm('Delete this note? This can\'t be undone.')) return;
			fetch(`/api/notes/${btn.dataset.noteId}/delete`, { method: 'POST' })
				.then((res) => { if (res.ok) window.location.reload(); });
		});
	});

	// --- Detail page: auto-save title/content, plus the same three actions ---
	const titleInput = document.getElementById('note-title-input');
	const contentInput = document.getElementById('note-content-input');
	const saveStatus = document.getElementById('note-save-status');

	if (titleInput && contentInput && window.noteId) {
		let saveTimer = null;

		function save() {
			fetch(`/api/notes/${window.noteId}/update`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ title: titleInput.value, content: contentInput.value }),
			}).then((res) => {
				if (saveStatus) saveStatus.textContent = res.ok ? 'Saved' : 'Could not save';
			});
		}

		function flushPendingSave() {
			if (!saveTimer) return;
			clearTimeout(saveTimer);
			saveTimer = null;
			// beforeunload/visibilitychange can't reliably wait on a fetch
			// before the page actually goes away - sendBeacon is the
			// browser-guaranteed way to fire a request that survives
			// navigating off the page, which matters a lot on mobile where
			// "navigating away" often means switching apps mid-sentence.
			const payload = JSON.stringify({ title: titleInput.value, content: contentInput.value });
			navigator.sendBeacon(
				`/api/notes/${window.noteId}/update`,
				new Blob([payload], { type: 'application/json' })
			);
		}

		function scheduleSave() {
			if (saveStatus) saveStatus.textContent = 'Saving\u2026';
			clearTimeout(saveTimer);
			saveTimer = setTimeout(save, 700);
		}

		titleInput.addEventListener('input', scheduleSave);
		contentInput.addEventListener('input', scheduleSave);

		document.addEventListener('visibilitychange', () => {
			if (document.visibilityState === 'hidden') flushPendingSave();
		});
		window.addEventListener('beforeunload', flushPendingSave);
	}

	document.getElementById('note-pin-btn')?.addEventListener('click', function () {
		fetch(`/api/notes/${this.dataset.noteId}/pin`, { method: 'POST' })
			.then((res) => res.json())
			.then((data) => {
				this.classList.toggle('active', !!data.pinned);
				const label = this.querySelector('.icon-btn-label');
				if (label) label.textContent = data.pinned ? 'Pinned' : 'Pin';
			});
	});

	document.getElementById('note-duplicate-btn')?.addEventListener('click', function () {
		fetch(`/api/notes/${this.dataset.noteId}/duplicate`, { method: 'POST' })
			.then((res) => res.json())
			.then((data) => { if (data.id) window.location.href = `/notes/${data.id}`; });
	});

	document.getElementById('note-delete-btn')?.addEventListener('click', function () {
		if (!confirm('Delete this note? This can\'t be undone.')) return;
		fetch(`/api/notes/${this.dataset.noteId}/delete`, { method: 'POST' })
			.then((res) => { if (res.ok) window.location.href = '/notes'; });
	});
})();
