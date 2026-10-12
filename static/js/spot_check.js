// Spot check page: scan a device and confirm who has it, or scan a
// student's ID first and then the device. Each finished check is saved by
// /admin/spot_check/record; a right-student result clears itself after a
// moment so the next scan can follow straight away.
(function () {
  const $ = function (id) { return document.getElementById(id); };
  const video = $('scVideo'), frame = $('scFrame'), statusEl = $('scStatus'), resultEl = $('scResult');
  const startBtn = $('scStart'), stopBtn = $('scStop'), torchBtn = $('scTorch');
  const csrf = (document.querySelector('meta[name=csrf-token]') || {}).content;
  let scanner = null, busy = false, torchOn = false, clearTimer = null;
  // What we're in the middle of: {device} waiting for who has it, or {person} waiting for their device.
  let pending = null;
  let counts = { checked: parseInt($('scCount').textContent, 10) || 0, problems: parseInt($('scProblems').textContent, 10) || 0 };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function setStatus(text) { statusEl.textContent = text; }
  function who(p) {
    if (!p) return 'no one';
    return esc(p.name) + (p.grad_year ? ' <span class="sc-muted">· class of ' + p.grad_year + '</span>' : '')
      + (p.site ? ' <span class="sc-muted">· ' + esc(p.site) + '</span>' : '');
  }
  function updateCounts(problem) {
    counts.checked += 1; if (problem) counts.problems += 1;
    $('scCount').textContent = counts.checked + ' checked';
    $('scProblems').textContent = counts.problems + ' problem' + (counts.problems === 1 ? '' : 's');
    $('scProblems').className = 'badge ' + (counts.problems ? 'badge-red' : 'badge-neutral');
  }

  async function getJSON(url) {
    const r = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (r.redirected || r.status === 401 || r.status === 302) throw new Error('Your session ended. Sign in again.');
    if (!r.ok) throw new Error('Lookup failed (' + r.status + ').');
    return r.json();
  }
  async function postJSON(url, body) {
    const r = await fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, Accept: 'application/json' }, body: JSON.stringify(body) });
    const data = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(data.error || 'Saving failed (' + r.status + ').');
    return data;
  }

  // ─── a scan came in ──────────────────────────────────────────────────────
  async function handle(code) {
    if (busy) return;
    busy = true;
    clearTimeout(clearTimer);
    try {
      const hit = await getJSON('/admin/spot_check/lookup?code=' + encodeURIComponent(code));
      if (hit.kind === 'device') {
        if (pending && pending.person) {
          await record(hit, pending.person.id);
        } else {
          window.FoxScanner.feedback(false);
          showDevice(hit);
        }
      } else if (hit.kind === 'person') {
        window.FoxScanner.feedback(false);
        if (pending && pending.device) await record(pending.device, hit.id);
        else showPerson(hit);
      } else {
        window.FoxScanner.feedback(true);
        showUnknown(hit.code);
      }
    } catch (e) {
      resultEl.innerHTML = '<div class="sc-banner sc-warn"><h2>Couldn\'t check that</h2>' + esc(e.message) + '</div>';
    } finally {
      busy = false;
    }
  }

  function showDevice(d) {
    pending = { device: d };
    let html = '<p class="sc-muted" style="margin:0;">Device</p><p class="sc-tag">' + esc(d.asset_tag) + '</p>';
    if (d.model) html += '<p class="sc-muted" style="margin:.1rem 0 .8rem;">' + esc(d.model) + '</p>';
    if (d.is_equipment) html += '<div class="sc-hint">This is equipment, not a student device.</div>';
    if (d.expected) {
      html += '<p class="sc-muted" style="margin:0;">Should be with' + (d.how === 'loaner' ? ' (loaner)' : '') + '</p>' +
        '<p class="sc-who">' + who(d.expected) + '</p>';
    } else {
      html += '<p class="sc-who">Not assigned to anyone</p>';
    }
    if (d.signin && d.signin.email && !d.signin.same && d.expected) {
      html += '<div class="sc-hint">Last Google sign-in: ' + esc(d.signin.name || d.signin.email) + '</div>';
    }
    html += '<div class="sc-actions">';
    if (d.expected) {
      html += '<button type="button" class="btn btn-primary" data-act="match">✓ It\'s with ' + esc(d.expected.first_name) + '</button>';
    }
    html += '<button type="button" class="btn btn-ghost" data-act="other">' + (d.expected ? 'Someone else has it' : 'Who has it?') + '</button>' +
      '<button type="button" class="btn btn-ghost" data-act="skip">Skip</button></div>' +
      '<div id="scPick" hidden><p class="sc-muted" style="margin:.8rem 0 .3rem;">Scan their ID card, or search:</p>' +
      '<input id="scSearch" placeholder="Name or ID number" autocomplete="off" data-scan="off"><div class="sc-people" id="scPeople"></div></div>';
    resultEl.innerHTML = html;
    setStatus(d.expected ? 'Confirm who has it, or scan their ID card.' : 'Scan the ID card of whoever has it, or search.');
  }

  function showPerson(p) {
    pending = { person: p };
    resultEl.innerHTML = '<p class="sc-muted" style="margin:0;">Student</p><p class="sc-who">' + who(p) + '</p>' +
      '<p class="sc-muted">' + (p.devices.length ? 'Should have: ' + p.devices.map(esc).join(', ') : 'Has no device checked out.') + '</p>' +
      '<div class="sc-actions"><button type="button" class="btn btn-ghost" data-act="skip">Cancel</button></div>';
    setStatus('Now scan the device ' + p.first_name + ' has.');
  }

  function showUnknown(code) {
    pending = null;
    resultEl.innerHTML = '<div class="sc-banner sc-warn"><h2>Not in FoxDesk</h2>"' + esc(code) + '" isn\'t a device tag, serial or ' +
      'ID number FoxDesk knows.</div><div class="sc-actions"><button type="button" class="btn btn-ghost" data-act="skip">OK</button></div>';
    setStatus('Scan the next one.');
  }

  async function record(device, foundId) {
    const res = await postJSON('/admin/spot_check/record', { code: device.asset_tag, found_person_id: foundId || null });
    pending = null;
    const ok = res.result === 'match';
    window.FoxScanner.feedback(!ok);
    updateCounts(!ok);
    let html;
    if (ok) {
      html = '<div class="sc-banner sc-ok"><h2>✓ Right student</h2>' + esc(device.asset_tag) + ' is with ' + esc(res.found ? res.found.name : '') + '.</div>';
    } else if (res.result === 'unassigned_in_use') {
      html = '<div class="sc-banner sc-warn"><h2>Not assigned to anyone</h2>' + esc(device.asset_tag) + ' is with ' +
        esc(res.found ? res.found.name : 'someone') + ', but FoxDesk doesn\'t have it assigned.</div>';
    } else {
      html = '<div class="sc-banner sc-bad"><h2>✗ ' + esc(res.label) + '</h2>' + esc(device.asset_tag) + ' belongs to <strong>' +
        esc(res.expected ? res.expected.name : 'someone else') + '</strong>, but <strong>' + esc(res.found ? res.found.name : 'someone') +
        '</strong> has it.' + (res.found && res.found.devices.length ? '<br>' + esc(res.found.first_name) + '\'s own device: ' +
        res.found.devices.map(esc).join(', ') : '') + '</div>';
    }
    html += '<div class="sc-actions">';
    if (!ok) html += '<a class="btn btn-ghost" href="/admin/assets/' + encodeURIComponent(device.asset_tag) + '/assign">Open the device</a>';
    html += '<button type="button" class="btn btn-primary" data-act="skip">Next</button></div>';
    resultEl.innerHTML = html;
    setStatus(ok ? 'Saved. Scan the next one.' : 'Saved as a problem. Tap Next when ready.');
    if (ok) clearTimer = setTimeout(reset, 3500);
  }

  function reset() {
    pending = null;
    resultEl.innerHTML = '<p class="sc-muted" style="margin:0;">Scan the next device or ID card.</p>';
    setStatus(scanner ? 'Point the camera at a label or ID card' : 'Camera is off.');
  }

  // ─── buttons in the result card ──────────────────────────────────────────
  let searchTimer = null;
  resultEl.addEventListener('click', async function (e) {
    const btn = e.target.closest('[data-act], [data-person]');
    if (!btn || busy) return;
    if (btn.dataset.person && pending && pending.device) {
      busy = true;
      try { await record(pending.device, btn.dataset.person); } catch (err) { setStatus(err.message); } finally { busy = false; }
      return;
    }
    const act = btn.dataset.act;
    if (act === 'skip') return reset();
    if (act === 'match' && pending && pending.device) {
      busy = true;
      try { await record(pending.device, null); } catch (err) { setStatus(err.message); } finally { busy = false; }
    }
    if (act === 'other') {
      $('scPick').hidden = false;
      $('scSearch').focus();
    }
  });
  resultEl.addEventListener('input', function (e) {
    if (e.target.id !== 'scSearch') return;
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    searchTimer = setTimeout(async function () {
      const list = $('scPeople');
      if (!list) return;
      if (q.length < 2) { list.innerHTML = ''; return; }
      try {
        const data = await getJSON('/admin/spot_check/people?q=' + encodeURIComponent(q));
        list.innerHTML = data.people.map(function (p) {
          return '<button type="button" data-person="' + p.id + '">' + esc(p.name) +
            (p.grad_year ? ' <span class="sc-muted">· ' + p.grad_year + '</span>' : '') + (p.site ? ' <span class="sc-muted">· ' + esc(p.site) + '</span>' : '') + '</button>';
        }).join('') || '<p class="sc-muted">No one matches.</p>';
      } catch (err) { list.innerHTML = '<p class="sc-muted">' + esc(err.message) + '</p>'; }
    }, 250);
  });

  // ─── camera and typed codes ──────────────────────────────────────────────
  startBtn.addEventListener('click', async function () {
    startBtn.disabled = true;
    try {
      frame.hidden = false;
      scanner = await window.FoxScanner.start(video, { onCode: handle, onStatus: setStatus });
      startBtn.hidden = true; stopBtn.hidden = false; torchBtn.hidden = !scanner.hasTorch;
    } catch (e) {
      frame.hidden = true;
      setStatus(e.message);
    } finally {
      startBtn.disabled = false;
    }
  });
  stopBtn.addEventListener('click', function () {
    if (scanner) scanner.stop();
    scanner = null; frame.hidden = true;
    startBtn.hidden = false; stopBtn.hidden = true; torchBtn.hidden = true;
    setStatus('Camera is off.');
  });
  torchBtn.addEventListener('click', async function () {
    if (scanner && await scanner.setTorch(!torchOn)) {
      torchOn = !torchOn;
      torchBtn.setAttribute('aria-pressed', String(torchOn));
    }
  });
  $('scManual').addEventListener('submit', function (e) {
    e.preventDefault();
    const code = $('scCode').value.trim();
    if (code) { $('scCode').value = ''; handle(code); }
  });
  window.addEventListener('pagehide', function () { if (scanner) scanner.stop(); });
})();
