document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.copy-btn');
  // Only buttons that carry their text in data-copy-text are handled here;
  // pages with their own copy logic (e.g. the prompt builder) reuse the
  // .copy-btn look without being hijacked - this used to overwrite their
  // clipboard with an empty string.
  if (!btn || !('copyText' in btn.dataset)) return;

  const text = btn.dataset.copyText || '';
  try {
    await navigator.clipboard.writeText(text);
    const original = btn.textContent;
    btn.textContent = 'Copied';
    btn.classList.add('copied');
    setTimeout(() => {
      btn.textContent = original;
      btn.classList.remove('copied');
    }, 1200);
  } catch (err) {
    // Fallback for contexts where the Clipboard API is unavailable
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1200);
  }
});
