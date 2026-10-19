// Shared by the website and bundled reader. No network calls or external assets.
export function installImageViewer(root) {
  const doc = root.ownerDocument || root;
  const dialog = doc.createElement('dialog');
  dialog.className = 'docs-image-viewer';
  dialog.setAttribute('aria-label', 'Enlarged documentation image');
  dialog.innerHTML = '<div class="docs-image-toolbar"><button type="button" data-size>Actual size</button><button type="button" data-close autofocus>Close</button></div><div class="docs-image-viewport" tabindex="0" role="region" aria-label="Image viewport"><img></div><p></p>';
  const style = doc.createElement('style');
  style.textContent = `
    [data-docs-zoom]{cursor:zoom-in}
    [data-docs-zoom]:focus-visible{outline:2px solid currentColor;outline-offset:4px}
    .docs-image-viewer{box-sizing:border-box;width:calc(100vw - 2rem);max-width:calc(100vw - 2rem);max-height:calc(100dvh - 2rem);padding:1rem;border:1px solid #777;border-radius:8px;background:var(--pl-color-bg,var(--vp-c-bg,#fff));color:var(--pl-color-fg,var(--vp-c-text-1,#222))}
    .docs-image-viewer::backdrop{background:rgb(0 0 0 / .8)}
    .docs-image-toolbar{display:flex;justify-content:flex-end;gap:.75rem;margin-bottom:1rem}
    .docs-image-toolbar button{font:inherit;color:inherit;background:transparent;border:1px solid currentColor;border-radius:4px;padding:.5rem 1rem;min-height:44px;cursor:pointer}
    .docs-image-viewport{overflow:auto;max-height:calc(100dvh - 12rem)}
    .docs-image-viewport img{display:block;width:auto;max-width:100%;max-height:calc(100dvh - 12rem);margin:auto;object-fit:contain}
    .docs-image-viewport.actual img{max-width:none;max-height:none}
    .docs-image-viewer p{margin:1rem 0 0;font:14px/1.5 system-ui}
  `;
  doc.head.append(style);
  doc.body.append(dialog);
  const viewport = dialog.querySelector('.docs-image-viewport');
  const enlarged = dialog.querySelector('img');
  const size = dialog.querySelector('[data-size]');
  let opener;
  const refresh = () => root.querySelectorAll('img:not([data-docs-zoom])').forEach(img => {
    if (dialog.contains(img)) return;
    img.dataset.docsZoom = '';
    img.tabIndex = 0;
    img.setAttribute('role', 'button');
    img.setAttribute('aria-haspopup', 'dialog');
    img.setAttribute('aria-label', `Enlarge image: ${img.alt || 'Documentation image'}`);
  });
  const open = event => {
    const img = event.target.closest?.('img[data-docs-zoom]');
    if (!img || !root.contains(img)) return;
    if (event.type === 'keydown' && !['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    opener = img;
    enlarged.src = img.currentSrc || img.src;
    enlarged.alt = img.alt;
    dialog.querySelector('p').textContent = img.closest('figure')?.querySelector('figcaption')?.textContent || img.alt;
    dialog.showModal();
  };
  root.addEventListener('click', open, true);
  root.addEventListener('keydown', open, true);
  dialog.querySelector('[data-close]').onclick = () => dialog.close();
  size.onclick = () => { size.textContent = viewport.classList.toggle('actual') ? 'Fit image' : 'Actual size'; };
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener('close', () => {
    viewport.classList.remove('actual');
    size.textContent = 'Actual size';
    enlarged.removeAttribute('src');
    if (opener?.isConnected) opener.focus();
  });
  const observer = new MutationObserver(refresh);
  observer.observe(root, {childList:true, subtree:true});
  refresh();
  return () => {
    observer.disconnect();
    root.removeEventListener('click', open, true);
    root.removeEventListener('keydown', open, true);
    dialog.remove();
    style.remove();
  };
}
