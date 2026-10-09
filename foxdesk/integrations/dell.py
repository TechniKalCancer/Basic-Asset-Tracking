"""Dell warranty lookup through Dell's TechDirect Warranty API.

Devices with a Dell-style service tag (7 letters/digits) are looked up 100
at a time. For each one Dell recognises, the device gets:
  - warranty_expiration: the latest end date of any of its entitlements,
    unless FoxDesk already has a later date (an extension bought elsewhere);
  - purchase_date: Dell's ship date, only when it's blank;
  - a 'dell' DeviceRecord holding the details (product, service level,
    ship date) for the device page.
Tags Dell doesn't recognise (another brand that happens to use 7-character
serials) are remembered too, so they aren't asked about every night.
A device is looked up again after RECHECK_DAYS (warranties get extended).
"""
import re
from datetime import date, datetime, timedelta

import requests

from foxdesk.core import DELL_CLIENT_ID, DELL_CLIENT_SECRET, db, logger
from foxdesk.models import AssetRegistry, DeviceRecord, WarrantySettings
from foxdesk.services.auth import _log_activity
from foxdesk.services.secrets import decrypt_secret, encrypt_secret

TOKEN_URL = 'https://apigtwb2c.us.dell.com/auth/oauth/v2/token'
WARRANTY_URL = 'https://apigtwb2c.us.dell.com/PROD/sbil/eapi/v5/asset-entitlements'
SECRET_PURPOSE = 'dell-client-secret'
BATCH = 100
RECHECK_DAYS = 30
NOT_DELL_RECHECK_DAYS = 180
SERVICE_TAG = re.compile(r'^[A-Za-z0-9]{7}$')


class WarrantyError(Exception):
    """A Dell API problem, worded for the admin."""


def warranty_settings():
    row = WarrantySettings.query.get(1)
    if row is None:
        row = WarrantySettings(id=1)
        db.session.add(row)
        db.session.flush()
    return row


def dell_credentials():
    """(client_id, client_secret, sources) — saved on the page, else .env."""
    row = WarrantySettings.query.get(1)
    saved_secret = decrypt_secret(row.dell_client_secret, SECRET_PURPOSE) if row and row.dell_client_secret else None
    client_id = (row.dell_client_id if row else None) or DELL_CLIENT_ID
    secret = saved_secret or DELL_CLIENT_SECRET
    sources = {'client_id': 'saved' if row and row.dell_client_id else ('env' if DELL_CLIENT_ID else None),
               'secret': 'saved' if saved_secret else ('env' if DELL_CLIENT_SECRET else None),
               'secret_unreadable': bool(row and row.dell_client_secret and saved_secret is None)}
    return client_id, secret, sources


def dell_configured():
    client_id, secret, _ = dell_credentials()
    return bool(client_id and secret)


def save_credentials(client_id, secret):
    row = warranty_settings()
    row.dell_client_id = (client_id or '').strip() or None
    if secret:
        row.dell_client_secret = encrypt_secret(secret.strip(), SECRET_PURPOSE)


# ─── Dell API ─────────────────────────────────────────────────────────────────

def _post_token(client_id, secret):
    return requests.post(TOKEN_URL, data={'grant_type': 'client_credentials', 'client_id': client_id,
                                          'client_secret': secret}, timeout=20)


def _get_entitlements(token, tags):
    return requests.get(WARRANTY_URL, params={'servicetags': ','.join(tags)},
                        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, timeout=60)


def _explain(response, what):
    if response.status_code in (401, 403):
        return f'Dell rejected the API key while {what}. Check the client ID and secret.'
    if response.status_code == 429:
        return f'Dell is rate-limiting this key ({what}). Try again later.'
    return f'Dell answered {response.status_code} while {what}.'


def get_token():
    client_id, secret, _ = dell_credentials()
    if not (client_id and secret):
        raise WarrantyError('Enter the Dell TechDirect client ID and secret first.')
    try:
        response = _post_token(client_id, secret)
    except requests.RequestException as e:
        raise WarrantyError(f'Couldn\'t reach Dell: {e}')
    if response.status_code != 200:
        raise WarrantyError(_explain(response, 'signing in'))
    token = (response.json() or {}).get('access_token')
    if not token:
        raise WarrantyError('Dell didn\'t return an access token.')
    return token


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def parse_asset(item):
    """One asset from Dell's response, as a plain dict."""
    entitlements = item.get('entitlements') or []
    ends = [(e, _day(e.get('endDate'))) for e in entitlements]
    ends = [(e, d) for e, d in ends if d]
    best = max(ends, key=lambda x: x[1]) if ends else (None, None)
    return dict(tag=(item.get('serviceTag') or '').upper(), invalid=bool(item.get('invalid')),
                product=item.get('productLineDescription') or item.get('systemDescription'),
                ship_date=_day(item.get('shipDate')), end_date=best[1],
                service_level=(best[0] or {}).get('serviceLevelDescription'),
                entitlements=len(entitlements))


def fetch_warranties(token, tags):
    try:
        response = _get_entitlements(token, tags)
    except requests.RequestException as e:
        raise WarrantyError(f'Couldn\'t reach Dell: {e}')
    if response.status_code != 200:
        raise WarrantyError(_explain(response, 'looking up warranties'))
    return [parse_asset(item) for item in (response.json() or [])]


# ─── which devices, and saving results ────────────────────────────────────────

def candidates(force=False):
    """Registry rows with a Dell-style service tag that are due a lookup."""
    now = datetime.utcnow()
    last = {r.registry_id: r for r in DeviceRecord.query.filter_by(source='dell') if r.registry_id}
    due = []
    for row in AssetRegistry.query.filter(AssetRegistry.serial_number.isnot(None)).order_by(AssetRegistry.id):
        serial = row.serial_number.strip()
        if not SERVICE_TAG.match(serial):
            continue
        rec = last.get(row.id)
        if rec and not force and rec.last_synced_at:
            wait = NOT_DELL_RECHECK_DAYS if (rec.raw or {}).get('not_dell') else RECHECK_DAYS
            if now - rec.last_synced_at < timedelta(days=wait):
                continue
        due.append(row)
    return due


def _save(row, info, now):
    rec = DeviceRecord.query.filter_by(source='dell', external_key=info['tag']).first()
    if rec is None:
        rec = DeviceRecord(source='dell', external_key=info['tag'])
        db.session.add(rec)
    rec.registry_id, rec.serial_number, rec.last_synced_at = row.id, info['tag'], now
    if info['invalid']:
        rec.raw = {'not_dell': True}
        return 'not_dell'
    rec.raw = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in info.items()
               if k in ('product', 'ship_date', 'end_date', 'service_level', 'entitlements') and v}
    result = 'no_warranty'
    if info['end_date'] and (not row.warranty_expiration or info['end_date'] > row.warranty_expiration):
        row.warranty_expiration = info['end_date']
        result = 'updated'
    elif info['end_date']:
        result = 'unchanged'
    if info['ship_date'] and not row.purchase_date:
        row.purchase_date = info['ship_date']
    return result


def run_dell_lookup(limit=None, force=False):
    """Look up due devices (at most `limit`). Commits after each batch, so
    a failure partway keeps what was already saved. Returns the summary."""
    settings = warranty_settings()
    rows = candidates(force)
    remaining_before = len(rows)
    if limit:
        rows = rows[:limit]
    summary = dict(checked=0, updated=0, unchanged=0, no_warranty=0, not_dell=0, missing=0)
    if rows:
        token = get_token()
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            by_tag = {r.serial_number.strip().upper(): r for r in batch}
            results = fetch_warranties(token, list(by_tag))
            now = datetime.utcnow()
            seen = set()
            for info in results:
                row = by_tag.get(info['tag'])
                if row is None:
                    continue
                seen.add(info['tag'])
                summary[_save(row, info, now)] += 1
            summary['missing'] += len(set(by_tag) - seen)
            summary['checked'] += len(batch)
            db.session.commit()
    summary['remaining'] = remaining_before - summary['checked']
    settings = warranty_settings()
    settings.last_run_at, settings.last_summary, settings.last_error = datetime.utcnow(), summary, None
    _log_activity('warranty_lookup', 'Dell warranty lookup: ' + describe_summary(summary))
    db.session.commit()
    logger.info('Dell warranty lookup: %s', summary)
    return summary


def describe_summary(s):
    text = (f'{s.get("checked", 0)} checked: {s.get("updated", 0)} warranty dates updated, '
            f'{s.get("unchanged", 0)} already current, {s.get("not_dell", 0)} not Dell')
    if s.get('no_warranty'):
        text += f', {s["no_warranty"]} with no warranty on file'
    if s.get('remaining'):
        text += f'. {s["remaining"]} still to look up'
    return text + '.'
