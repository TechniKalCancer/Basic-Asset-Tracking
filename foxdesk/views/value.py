"""Pages: device value (by funding source, school, type, year), filling in
purchase info in bulk, and repair cost by model."""
import csv
import io
from datetime import date

from flask import Response, flash, redirect, render_template, request, url_for

from foxdesk.core import app, db
from foxdesk.models import END_USER_TYPES, EQUIPMENT_TYPES
from foxdesk.services.asset_value import (
    GROUPS, fill_purchase_info, life_for, model_names, repair_costs, school_year_start, value_report, value_settings,
)
from foxdesk.services.auth import _current_site_ids, _has_permission, _log_activity, require_permission
from foxdesk.services.scoping import _sites_for_actor
from foxdesk.services.util import _parse_date, _parse_money

TYPES = END_USER_TYPES + EQUIPMENT_TYPES


def _csv(name, header, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename="{name}"'})


@app.route('/admin/value')
@require_permission('devices')
def admin_value():
    group = request.args.get('group', 'funding')
    group = group if group in GROUPS else 'funding'
    report = value_report(_current_site_ids(), group)
    if request.args.get('export'):
        return _csv(f'device-value-by-{group}.csv', [GROUPS[group], 'Devices', 'Cost', 'Value now', 'Fully used'],
                    [[r['key'], r['count'], f'{r["cost"]:.2f}', f'{r["value"]:.2f}', r['fully']] for r in report['rows']])
    settings = value_settings()
    return render_template('admin_value.html', report=report, group=group, groups=GROUPS, settings=settings,
                           lives={t: life_for(t, settings) for t in TYPES}, types=TYPES, models=model_names(),
                           sites=_sites_for_actor(_current_site_ids()), can_fill=_has_permission('devices_manage'),
                           can_admin=_has_permission('admin'))


@app.route('/admin/value/settings', methods=['POST'])
@require_permission('admin')
def admin_value_settings():
    row = value_settings(create=True)
    lives = {}
    for t in TYPES:
        years = request.form.get(f'life_{t}', type=int)
        if years and 1 <= years <= 30:
            lives[t] = years
    row.life_years = lives
    salvage = request.form.get('salvage_percent', type=int) or 0
    row.salvage_percent = min(max(salvage, 0), 90)
    row.labor_rate = _parse_money(request.form.get('labor_rate'))
    _log_activity('settings_edit', 'Updated device useful lives, salvage and the repair labor rate.')
    db.session.commit()
    flash('Saved.', 'success')
    return redirect(url_for('admin_value'))


@app.route('/admin/value/fill', methods=['POST'])
@require_permission('devices_manage')
def admin_value_fill():
    match = dict(model=request.form.get('model', '').strip() or None,
                 device_type=request.form.get('device_type', '').strip() or None,
                 site_id=request.form.get('site_id', type=int))
    if not any(match.values()):
        flash('Pick which devices: a model, a type or a school.', 'error')
        return redirect(url_for('admin_value'))
    values = dict(purchase_date=_parse_date(request.form.get('purchase_date')),
                  purchase_cost=_parse_money(request.form.get('purchase_cost')),
                  funding_source=request.form.get('funding_source', '').strip()[:80] or None,
                  funding_year=request.form.get('funding_year', type=int))
    if not any(v not in (None, '') for v in values.values()):
        flash('Fill in at least one thing to set.', 'error')
        return redirect(url_for('admin_value'))
    if values['purchase_date'] and values['purchase_date'] > date.today():
        flash('The purchase date is in the future.', 'error')
        return redirect(url_for('admin_value'))
    overwrite = bool(request.form.get('overwrite'))
    changed = fill_purchase_info(_current_site_ids(), match, values, overwrite)
    which = ', '.join(str(v) for v in match.values() if v)
    _log_activity('registry_bulk_edit', f'Set purchase info on {changed} device(s) ({which})'
                  + (' — replacing what was there' if overwrite else '') + '.')
    db.session.commit()
    flash(f'Updated {changed} device{"" if changed == 1 else "s"}.' + ('' if overwrite else ' Fields already filled in were left alone.'),
          'success' if changed else 'info')
    return redirect(url_for('admin_value'))


@app.route('/admin/repairs/costs')
@require_permission('repairs')
def admin_repair_costs():
    period = request.args.get('period', 'year')
    since = school_year_start() if period == 'year' else None
    rows = repair_costs(_current_site_ids(), since)
    if request.args.get('export'):
        return _csv('repair-cost-by-model.csv',
                    ['Model', 'Devices', 'Repairs', 'Per 100 devices', 'Parts', 'Labor hours', 'Labor cost', 'Total cost',
                     'Billed to families', 'Average days out', 'Outcomes'],
                    [[r['model'], r['devices'], r['repairs'], r['per100'] if r['per100'] is not None else '', f'{r["parts"]:.2f}',
                      r['hours'], f'{r["labor"]:.2f}', f'{r["cost"]:.2f}', f'{r["billed"]:.2f}',
                      r['avg_days'] if r['avg_days'] is not None else '',
                      '; '.join(f'{k} {v}' for k, v in r['outcomes'].items())] for r in rows])
    return render_template('admin_repair_costs.html', rows=rows, period=period, since=since, settings=value_settings())
