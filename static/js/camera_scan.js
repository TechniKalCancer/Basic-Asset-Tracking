// Phone/tablet camera barcode scanning for every "scan or type" field.
// Adds a camera "Scan" button beside each matching input; tapping it opens a
// full-screen camera view, and the first barcode read is typed into the
// field exactly like a USB scanner would (value + Enter), so each page's
// existing scan handling just works. Uses the browser's built-in
// BarcodeDetector (Chrome/Edge/ChromeOS/Android) and lazy-loads ZXing only
// where that's missing (iPhone/iPad Safari). Cameras require HTTPS.
(function () {
  const SELECTOR = [
    'input[data-scan]', 'input[name="scan_value"]', 'input#barcode', 'input#scanInput',
    'input[name="asset_tag"]:not([disabled]):not([type="hidden"])', 'input[name="loaner_asset_tag"]',
    'input#loanerAssetTagInput', 'input[name="serial_number"]', 'input[name="value"][placeholder*="Scan"]',
  ].join(',');
  const WANTED_FORMATS = ['code_128', 'code_39', 'code_93', 'codabar', 'ean_13', 'ean_8', 'upc_a', 'upc_e', 'itf', 'qr_code', 'data_matrix'];
  const ZXING_URL = 'https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js';

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (window.isSecureContext) return;  // genuinely no camera API — nothing to offer
  }

  let overlay, video, statusEl, stream, stopLoop, zxingReader;

  function buildOverlay() {
    overlay = document.createElement('div');
    overlay.className = 'cam-overlay';
    overlay.innerHTML =
      '<div class="cam-frame"><video playsinline muted></video><div class="cam-reticle"></div></div>' +
      '<p class="cam-status">Starting camera…</p>' +
      '<button type="button" class="btn btn-ghost cam-close">Cancel</button>';
    document.body.appendChild(overlay);
    video = overlay.querySelector('video');
    statusEl = overlay.querySelector('.cam-status');
    overlay.querySelector('.cam-close').addEventListener('click', close);
  }

  function close() {
    if (stopLoop) { stopLoop(); stopLoop = null; }
    if (zxingReader) { try { zxingReader.reset(); } catch (e) {} zxingReader = null; }
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    if (overlay) overlay.classList.remove('open');
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      const s = document.createElement('script');
      s.src = src; s.onload = resolve; s.onerror = function () { reject(new Error('Could not load the scanner library.')); };
      document.head.appendChild(s);
    });
  }

  function feedback() {
    try { navigator.vibrate && navigator.vibrate(80); } catch (e) {}
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator(); const vol = ctx.createGain();
      osc.connect(vol); vol.connect(ctx.destination);
      osc.frequency.value = 1200; vol.gain.value = 0.15;
      osc.start(); osc.stop(ctx.currentTime + 0.08);
    } catch (e) {}
  }

  // Type the value in the way a USB scanner would: set it, fire input/change,
  // then Enter. Pages with their own Enter handler (check in/out, loaners)
  // take it from there; a plain single-field scan form gets submitted.
  function deliver(input, value) {
    input.value = value.trim();
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
    const down = new KeyboardEvent('keydown', opts);
    const press = new KeyboardEvent('keypress', opts);
    const handled = !input.dispatchEvent(down) | !input.dispatchEvent(press);
    input.dispatchEvent(new KeyboardEvent('keyup', opts));
    const form = input.form;
    if (!handled && form) {
      const fields = Array.from(form.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=file]), select, textarea'))
        .filter(function (el) { return el.offsetParent !== null; });
      if (fields.length === 1 && form.checkValidity()) {
        form.requestSubmit ? form.requestSubmit() : form.submit();
        return;
      }
    }
    input.focus();
  }

  async function open(input) {
    if (!window.isSecureContext) {
      alert('Camera scanning only works over HTTPS. Ask your admin to serve this site over https://, or type the tag in.');
      return;
    }
    if (!overlay) buildOverlay();
    overlay.classList.add('open');
    statusEl.textContent = 'Starting camera…';
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false,
      });
    } catch (e) {
      statusEl.textContent = e.name === 'NotAllowedError'
        ? 'Camera permission was denied — allow it in your browser\'s site settings and try again.'
        : 'Couldn\'t open a camera: ' + e.message;
      return;
    }
    video.srcObject = stream;
    await video.play().catch(function () {});
    statusEl.textContent = 'Point the camera at the barcode';

    function done(value) {
      feedback();
      close();
      deliver(input, value);
    }

    if ('BarcodeDetector' in window) {
      let formats = WANTED_FORMATS;
      try {
        const supported = await window.BarcodeDetector.getSupportedFormats();
        formats = WANTED_FORMATS.filter(function (f) { return supported.includes(f); });
      } catch (e) {}
      const detector = new window.BarcodeDetector({ formats: formats });
      let running = true;
      stopLoop = function () { running = false; };
      (async function loop() {
        while (running) {
          if (video.readyState >= 2) {
            try {
              const codes = await detector.detect(video);
              if (codes.length && running) { done(codes[0].rawValue); return; }
            } catch (e) {}
          }
          await new Promise(function (r) { setTimeout(r, 120); });
        }
      })();
      return;
    }

    try {
      if (!window.ZXing) { statusEl.textContent = 'Loading scanner…'; await loadScript(ZXING_URL); }
      statusEl.textContent = 'Point the camera at the barcode';
      // ZXing manages its own stream, so hand the camera over to it.
      stream.getTracks().forEach(function (t) { t.stop(); }); stream = null;
      zxingReader = new window.ZXing.BrowserMultiFormatReader();
      await zxingReader.decodeFromConstraints(
        { video: { facingMode: { ideal: 'environment' } } }, video,
        function (result) { if (result && zxingReader) done(result.getText()); }
      );
    } catch (e) {
      statusEl.textContent = e.message || 'Scanner failed to start.';
    }
  }

  function enhance(input) {
    if (input.dataset.camScan === 'done' || input.dataset.scan === 'off') return;
    input.dataset.camScan = 'done';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cam-btn';
    btn.title = 'Scan with camera';
    btn.setAttribute('aria-label', 'Scan barcode with camera');
    // A camera icon once static/icons/camera.svg exists (see icon() in
    // foxdesk/web.py); until then, a plain word.
    if ((window.APP_ICONS || []).indexOf('camera') !== -1) {
      const ic = document.createElement('span');
      ic.className = 'icon'; ic.style.setProperty('--icon', "url('/static/icons/camera.svg')"); ic.style.marginRight = '0';
      btn.appendChild(ic);
    } else {
      btn.textContent = 'Scan';
    }
    btn.addEventListener('click', function () { open(input); });
    const wrap = document.createElement('span');
    wrap.className = 'cam-wrap';
    // The wrapper takes over the input's own sizing (e.g. flex:1 in a
    // search row, a fixed width) so wrapping doesn't shift the layout.
    ['flex', 'width', 'minWidth', 'maxWidth'].forEach(function (prop) {
      if (input.style[prop]) { wrap.style[prop] = input.style[prop]; input.style[prop] = ''; }
    });
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    wrap.appendChild(btn);
  }

  function scan(root) { (root || document).querySelectorAll(SELECTOR).forEach(enhance); }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && overlay && overlay.classList.contains('open')) close(); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { scan(); });
  else scan();
  window.enhanceCameraScan = scan;
})();
