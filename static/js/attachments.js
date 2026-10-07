// Downscales photos picked in any <input type="file" data-photo-input>
// before the form submits — a modern phone photo is 3-8 MB, which is slow
// on school Wi-Fi and pointless for damage evidence. Re-encodes to a
// ≤1600px JPEG (~200-400 KB) via canvas and swaps it into the input with a
// DataTransfer. Anything the browser can't decode (a PDF, or HEIC outside
// Safari) is left untouched and the server decides whether to accept it.
(function () {
  const MAX_EDGE = 1600;
  const QUALITY = 0.82;

  function loadImage(file) {
    return new Promise(function (resolve, reject) {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('decode failed')); };
      img.src = url;
    });
  }

  async function shrink(file) {
    if (!file.type.startsWith('image/') || file.type === 'image/gif') return file;
    try {
      const img = await loadImage(file);
      const scale = Math.min(1, MAX_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
      if (scale === 1 && file.size < 600 * 1024) return file;
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(img.naturalWidth * scale);
      canvas.height = Math.round(img.naturalHeight * scale);
      canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise(function (r) { canvas.toBlob(r, 'image/jpeg', QUALITY); });
      if (!blob || blob.size >= file.size) return file;
      const name = file.name.replace(/\.[^.]+$/, '') + '.jpg';
      return new File([blob], name, { type: 'image/jpeg', lastModified: Date.now() });
    } catch (e) {
      return file;
    }
  }

  document.addEventListener('change', async function (e) {
    const input = e.target;
    if (!input.matches || !input.matches('input[type=file][data-photo-input]')) return;
    if (!input.files.length || typeof DataTransfer === 'undefined') return;
    const form = input.form;
    const submits = form ? form.querySelectorAll('button[type=submit]') : [];
    submits.forEach(function (b) { b.disabled = true; });
    try {
      const shrunk = await Promise.all(Array.from(input.files).map(shrink));
      const dt = new DataTransfer();
      shrunk.forEach(function (f) { dt.items.add(f); });
      input.files = dt.files;
    } finally {
      submits.forEach(function (b) { b.disabled = false; });
    }
  });
})();
