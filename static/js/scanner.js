// Continuous camera scanning for pages that read one code after another
// (spot checks). FoxScanner.start(video, {onCode, onStatus}) resolves to
// {stop(), setTorch(on), hasTorch}.
//
// Reads 1D barcodes (Code 128/39/93, Codabar, EAN, UPC, ITF) and 2D codes
// (QR, Data Matrix, PDF417 — the kind on many ID cards — and Aztec), using
// the browser's own BarcodeDetector where there is one (Chrome on Android
// and ChromeOS) and ZXing elsewhere (iPhone/iPad Safari). The same code
// read again within REPEAT_MS is ignored, so holding the camera on a label
// counts once. Cameras only work over HTTPS.
(function () {
  const FORMATS = ['code_128', 'code_39', 'code_93', 'codabar', 'ean_13', 'ean_8', 'upc_a', 'upc_e', 'itf',
                   'qr_code', 'data_matrix', 'pdf417', 'aztec'];
  const ZXING_URL = 'https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js';
  const REPEAT_MS = 2500;

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      const s = document.createElement('script');
      s.src = src; s.onload = resolve;
      s.onerror = function () { reject(new Error('Could not load the scanner library.')); };
      document.head.appendChild(s);
    });
  }

  function zxingHints() {
    const Z = window.ZXing, F = Z.BarcodeFormat;
    const hints = new Map();
    hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [F.CODE_128, F.CODE_39, F.CODE_93, F.CODABAR, F.EAN_13, F.EAN_8,
      F.UPC_A, F.UPC_E, F.ITF, F.QR_CODE, F.DATA_MATRIX, F.PDF_417, F.AZTEC]);
    hints.set(Z.DecodeHintType.TRY_HARDER, true);
    return hints;
  }

  async function start(video, opts) {
    const status = opts.onStatus || function () {};
    if (!window.isSecureContext) throw new Error('The camera only works when FoxDesk is opened over https://. Type the tag below instead.');
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('This browser can\'t use the camera.');

    let last = { code: null, at: 0 };
    let running = true;
    let stream = null, reader = null;
    function emit(code) {
      code = (code || '').trim();
      const now = Date.now();
      if (!code || !running || (code === last.code && now - last.at < REPEAT_MS)) return;
      last = { code: code, at: now };
      opts.onCode(code);
    }

    status('Starting camera…');
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false,
      });
    } catch (e) {
      throw new Error(e.name === 'NotAllowedError'
        ? 'Camera permission was denied. Allow the camera for this site in the browser settings, then try again.'
        : 'Couldn\'t open a camera: ' + e.message);
    }
    video.setAttribute('playsinline', ''); video.muted = true;
    video.srcObject = stream;
    await video.play().catch(function () {});
    const track = stream.getVideoTracks()[0];
    const caps = track && track.getCapabilities ? track.getCapabilities() : {};

    const controller = {
      hasTorch: !!caps.torch,
      stop: function () {
        running = false;
        if (reader) { try { reader.reset(); } catch (e) {} reader = null; }
        if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
        video.srcObject = null;
      },
      setTorch: async function (on) {
        try { await track.applyConstraints({ advanced: [{ torch: !!on }] }); return true; } catch (e) { return false; }
      },
    };

    if ('BarcodeDetector' in window) {
      let formats = FORMATS;
      try {
        const supported = await window.BarcodeDetector.getSupportedFormats();
        formats = FORMATS.filter(function (f) { return supported.indexOf(f) !== -1; });
      } catch (e) {}
      const detector = new window.BarcodeDetector({ formats: formats });
      status('Point the camera at a label or ID card');
      (async function loop() {
        while (running) {
          if (video.readyState >= 2) {
            try {
              const codes = await detector.detect(video);
              if (codes.length) emit(codes[0].rawValue);
            } catch (e) {}
          }
          await new Promise(function (r) { setTimeout(r, 150); });
        }
      })();
      return controller;
    }

    status('Loading scanner…');
    if (!window.ZXing) await loadScript(ZXING_URL);
    reader = new window.ZXing.BrowserMultiFormatReader(zxingHints(), 250);
    status('Point the camera at a label or ID card');
    const onResult = function (result) { if (result) emit(result.getText()); };
    if (typeof reader.decodeFromStream === 'function') {
      reader.decodeFromStream(stream, video, onResult);
    } else {
      // Older ZXing builds want their own stream (and then the flashlight can't be switched).
      stream.getTracks().forEach(function (t) { t.stop(); }); stream = null;
      controller.hasTorch = false;
      reader.decodeFromConstraints({ video: { facingMode: { ideal: 'environment' } } }, video, onResult);
    }
    return controller;
  }

  // A short beep (and buzz where phones allow it); `bad` is two low beeps.
  function feedback(bad) {
    try { navigator.vibrate && navigator.vibrate(bad ? [120, 80, 120] : 60); } catch (e) {}
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      (bad ? [0, 0.2] : [0]).forEach(function (at) {
        const osc = ctx.createOscillator(), vol = ctx.createGain();
        osc.connect(vol); vol.connect(ctx.destination);
        osc.frequency.value = bad ? 330 : 1200; vol.gain.value = 0.15;
        osc.start(ctx.currentTime + at); osc.stop(ctx.currentTime + at + (bad ? 0.15 : 0.08));
      });
    } catch (e) {}
  }

  window.FoxScanner = { start: start, feedback: feedback, FORMATS: FORMATS };
})();
