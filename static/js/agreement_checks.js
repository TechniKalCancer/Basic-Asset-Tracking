// Agreement checkboxes (People list and person page): ticking one records
// that a signed paper copy is on file; unticking removes a paper copy that
// was recorded by mistake. A box for an agreement signed in the portal is
// shown ticked and can't be unticked here.
(function () {
  const csrf = (document.querySelector('meta[name=csrf-token]') || {}).content;
  document.addEventListener('change', async function (e) {
    const box = e.target;
    if (!box.classList || !box.classList.contains('ag-check')) return;
    const undo = !box.checked;
    if (undo && !confirm('Remove the paper copy recorded for ' + box.dataset.who + '?')) {
      box.checked = true;
      return;
    }
    box.disabled = true;
    try {
      const r = await fetch(box.dataset.url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, Accept: 'application/json' },
        body: JSON.stringify({ action: undo ? 'undo' : 'sign' }),
      });
      const data = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(data.error || 'That didn\'t save (' + r.status + ').');
      box.checked = data.complete;
      box.title = data.complete ? 'Signed' + (data.signed_by ? ' by ' + data.signed_by : '') + (data.signed_at ? ', ' + data.signed_at : '')
        : 'Not signed';
      const note = document.getElementById(box.dataset.note || '');
      if (note) note.textContent = data.complete ? 'Signed' + (data.signed_by ? ' — ' + data.signed_by + ', ' + data.signed_at : '') : 'Not signed';
    } catch (err) {
      box.checked = !box.checked;
      alert(err.message);
    } finally {
      box.disabled = false;
    }
  });
})();
