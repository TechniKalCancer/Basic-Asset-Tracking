// Bulk-prints labels for a checkbox-selected set of assets, looping over the
// DYMO SDK's single-label print() call for each one in sequence. Shares the
// label template/builder with the single-print flow via dymo_label_core.js.
(function () {
    var statusEl     = document.getElementById('dymoStatus');
    var controlsEl   = document.getElementById('dymoControls');
    var selectEl     = document.getElementById('printerSelect');
    var printBtn     = document.getElementById('printSelectedBtn');
    var chargerCheck = document.getElementById('printChargerLabel');
    var progressEl   = document.getElementById('printProgress');
    var selectAllBox = document.getElementById('selectAll');

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

    function getSelectedRows() {
        var boxes = document.querySelectorAll('.asset-select:checked');
        var rows = [];
        boxes.forEach(function (box) {
            rows.push({ assetTag: box.dataset.assetTag, personName: box.dataset.personName || '' });
        });
        return rows;
    }

    // Prints one row (and optionally its charger label) then recurses to the
    // next, with a short delay between jobs so the spooler isn't hammered.
    function printQueue(rows, printerName, includeCharger, index) {
        if (index >= rows.length) {
            progressEl.textContent = 'Done — printed ' + rows.length + ' label' +
                (rows.length !== 1 ? 's' : '') + (includeCharger ? ' (plus charger labels)' : '') + '.';
            printBtn.disabled = false;
            return;
        }
        var row = rows[index];
        progressEl.textContent = 'Printing ' + (index + 1) + ' of ' + rows.length + ': ' + row.assetTag + '…';
        try {
            buildAssetLabel(row.assetTag, row.personName, false).print(printerName);
            if (includeCharger) {
                setTimeout(function () {
                    try {
                        buildAssetLabel(row.assetTag, row.personName, true).print(printerName);
                    } catch (e) {
                        // Keep going even if one charger label fails.
                    }
                }, 500);
            }
        } catch (e) {
            progressEl.textContent += ' (failed: ' + (e.message || e) + ')';
        }
        setTimeout(function () { printQueue(rows, printerName, includeCharger, index + 1); }, includeCharger ? 1300 : 800);
    }

    printBtn.addEventListener('click', function () {
        var rows = getSelectedRows();
        if (rows.length === 0) {
            alert('Select at least one asset to print.');
            return;
        }
        printBtn.disabled = true;
        printQueue(rows, selectEl.value, !!(chargerCheck && chargerCheck.checked), 0);
    });

    if (selectAllBox) {
        selectAllBox.addEventListener('change', function () {
            document.querySelectorAll('.asset-select').forEach(function (box) {
                box.checked = selectAllBox.checked;
            });
        });
    }

    init();
})();
