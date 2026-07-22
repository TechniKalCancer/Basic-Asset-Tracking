// Prints an asset tag + assigned name to a Dymo LabelWriter via the DYMO Connect
// Framework. Requires DYMO Connect (https://www.dymo.com) installed and running on
// this device, and its local web service certificate trusted once in the browser
// (visit https://127.0.0.1:41951 and accept the warning) — see README.
// Label building is shared with the bulk print page via dymo_label_core.js.
(function () {
    var statusEl     = document.getElementById('dymoStatus');
    var controlsEl   = document.getElementById('dymoControls');
    var selectEl     = document.getElementById('printerSelect');
    var previewImg   = document.getElementById('labelPreview');
    var printBtn     = document.getElementById('printLabelBtn');
    var chargerCheck = document.getElementById('printChargerLabel');

    function showPreview() {
        try {
            var label = buildAssetLabel(window.PRINT_LABEL_ASSET_TAG, window.PRINT_LABEL_PERSON_NAME, false);
            var png = label.render();
            previewImg.src = 'data:image/png;base64,' + png;
            previewImg.style.display = 'block';
        } catch (e) {
            // Preview is best-effort — printing still works without it.
        }
    }

    // Guards the whole detect-and-list-printers flow. init()'s callback fires
    // quickly regardless of whether DYMO Connect is actually reachable — the real
    // network call (and the place this can hang forever with no service running)
    // is getPrintersAsync(), so the timeout has to span both steps, not just init().
    var settled = false;
    var timeout;

    function settle(message) {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        if (message) statusEl.textContent = message;
    }

    function loadPrinters() {
        dymo.label.framework.getPrintersAsync().then(function (printers) {
            if (settled) return;
            var labelWriters = printers.filter(function (p) { return p.printerType === 'LabelWriterPrinter'; });
            var usable = labelWriters.length ? labelWriters : printers;
            if (usable.length === 0) {
                settle('DYMO Connect is running, but no printers were found. Check the printer is connected and powered on.');
                return;
            }
            usable.forEach(function (p) {
                var opt = document.createElement('option');
                opt.value = p.name;
                opt.textContent = p.name;
                selectEl.appendChild(opt);
            });
            settle();
            statusEl.style.display = 'none';
            controlsEl.style.display = 'block';
            showPreview();
        }).thenCatch(function (e) {
            settle('Could not list DYMO printers: ' + (e.message || e));
        });
    }

    function init() {
        if (typeof dymo === 'undefined' || !dymo.label || !dymo.label.framework) {
            statusEl.textContent = 'DYMO Connect script failed to load.';
            return;
        }

        timeout = setTimeout(function () {
            settle('DYMO Connect not detected. Install and start DYMO Connect on this device, ' +
                'then reload this page. (First time only: visit https://127.0.0.1:41951 once and accept the certificate warning.)');
        }, 5000);

        try {
            dymo.label.framework.init(function () {
                if (!settled) loadPrinters();
            });
        } catch (e) {
            settle('DYMO Connect not detected: ' + (e.message || e));
        }
    }

    printBtn.addEventListener('click', function () {
        var printerName = selectEl.value;
        try {
            buildAssetLabel(window.PRINT_LABEL_ASSET_TAG, window.PRINT_LABEL_PERSON_NAME, false).print(printerName);
        } catch (e) {
            alert('Print failed: ' + (e.message || e));
            return;
        }

        if (chargerCheck && chargerCheck.checked) {
            // Small delay so the two jobs don't hit the print spooler simultaneously.
            setTimeout(function () {
                try {
                    buildAssetLabel(window.PRINT_LABEL_ASSET_TAG, window.PRINT_LABEL_PERSON_NAME, true).print(printerName);
                } catch (e) {
                    alert('Charger label print failed: ' + (e.message || e));
                }
            }, 800);
        }
    });

    // Script tag is placed after the elements it needs, so the DOM is already
    // ready by the time this runs — no need to wait for a load/DOMContentLoaded
    // event (which may have already fired before this listener could attach).
    init();
})();
