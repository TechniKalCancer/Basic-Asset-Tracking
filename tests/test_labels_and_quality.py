"""Avery labels / Code 128 and the Data Quality checks."""
from conftest import A


def test_code128_patterns_are_well_formed():
    patterns = A._CODE128_PATTERNS
    assert len(patterns) == 107 and len(set(patterns)) == 107
    assert all(sum(map(int, p)) == 11 for p in patterns[:106]) and sum(map(int, patterns[106])) == 13


def test_code128_checksums():
    # Code B: (104 + 1*38 + 2*35 + 3*40 + 4*51 + 5*17 + 6*18 + 7*19 + 8*20) % 103 == 95
    assert A._code128_values('FCHS1234') == [104, 38, 35, 40, 51, 17, 18, 19, 20, 95, 106]
    # all-digit, even length -> Code C pairs
    assert A._code128_values('123456') == [105, 12, 34, 56, 44, 106]


def test_avery_sheet_skips_used_labels_and_adds_chargers(client, make):
    a, b = make.device(), make.device()
    r = client.post('/admin/labels/avery', data={'template': '5160', 'skip': '2', 'chargers': 'on',
                                                 'asset_tags': f'{a.asset_tag}\n{b.asset_tag}\nNOT-A-TAG'})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert body.count('class="label empty"') == 2 and body.count('class="label "') == 4
    assert client.post('/admin/labels/avery', data={'template': '9999', 'asset_tags': a.asset_tag}).status_code == 400


def checks_by_key():
    with A.app.test_request_context():
        from flask import session
        session.update(admin_logged_in=True, is_admin=True, is_super_admin=True)
        return {c['key']: c for c in A._data_quality_checks(None)}


def test_data_quality_finds_planted_problems(make):
    site = make.site()
    gone = make.person(active=False, site=site)
    make.device(serial='5CD-0001X', site=site)
    make.device(serial='5cd0001x', site=site)                      # same serial, different case/dash
    make.device(holder=gone, site=site)                             # held by an inactive person
    make.device(serial='', site=site)                               # no serial
    make.device(site=None)                                          # no site
    holder = make.person(site=site)
    A.Asset.query.filter_by(asset_tag=make.device(site=site).asset_tag).update({'status': 'assigned'})
    A.Asset.query.filter_by(asset_tag=make.device(site=site).asset_tag).update({'assigned_to_id': holder.id, 'status': 'available'})
    A.db.session.commit()
    c = checks_by_key()
    assert c['duplicate_serials']['count'] == 2
    assert c['inactive_holders']['count'] == 1
    assert c['missing_serial']['count'] == 1
    assert c['no_site']['count'] == 1
    assert c['status_mismatch']['count'] == 2


def test_data_quality_csv_export(client, make):
    make.device(serial='AAA1')
    make.device(serial='aaa1')
    r = client.get('/admin/data_quality?export=duplicate_serials')
    assert r.mimetype == 'text/csv' and 'AAA1' in r.get_data(as_text=True)
    assert client.get('/admin/data_quality?export=nope').status_code == 404
