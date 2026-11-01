(function () {
	'use strict';
	// Swipe on the image itself, not the whole page, so normal vertical
	// scrolling elsewhere (the info panel, folder dropdown, etc.) is untouched.
	const target = document.querySelector('.detail-image');
	if (!target) return;

	let startX = 0, startY = 0, startTime = 0;
	const THRESHOLD = 60;      // minimum horizontal distance, in px, to count as a swipe
	const MAX_VERTICAL = 80;   // if the finger moved more than this vertically, it's a scroll, not a swipe
	const MAX_TIME = 800;      // ignore slow drags - only a real flick counts

	target.addEventListener('touchstart', (e) => {
		if (e.touches.length !== 1) return;
		startX = e.touches[0].clientX;
		startY = e.touches[0].clientY;
		startTime = Date.now();
	}, { passive: true });

	target.addEventListener('touchend', (e) => {
		if (!startTime) return;
		const touch = e.changedTouches[0];
		const deltaX = touch.clientX - startX;
		const deltaY = touch.clientY - startY;
		const elapsed = Date.now() - startTime;
		startTime = 0;

		if (elapsed > MAX_TIME) return;
		if (Math.abs(deltaY) > MAX_VERTICAL) return;
		if (Math.abs(deltaX) < THRESHOLD) return;

		// Swipe left (finger moves toward the left edge) advances forward,
		// same direction as flipping through photos in a normal gallery app.
		const selector = deltaX < 0
			? '.detail-nav .btn:not(.btn-disabled):last-child'
			: '.detail-nav .btn:not(.btn-disabled):first-child';
		const link = document.querySelector(selector);
		if (link) window.location = link.href;
	}, { passive: true });
})();
