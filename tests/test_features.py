"""Settings → Features: switching modules off hides and blocks them everywhere."""
from conftest import A


def save(client, *off):
    """Submit the Features form with everything on except `off`."""
    data = {f'feature_{k}': 'on' for k in A.FEATURES if k not in off}
    return client.post('/admin/features', data=data)


def test_everything_on_by_default(client):
    body = client.get('/admin/features').get_data(as_text=True)
    assert body.count('type="checkbox"') == len(A.FEATURES)
    assert all(A.feature_switch(k) for k in A.FEATURES)


def test_turning_loaners_off_hides_and_blocks_it(client):
    save(client, 'loaners')
    assert A.FeatureToggle.query.get('loaners').enabled is False
    dash = client.get('/admin').get_data(as_text=True)
    assert '>Loaners<' not in dash.replace('</span>', '')  # nav tab gone
    r = client.get('/admin/loaners')
    assert r.status_code == 404 and b'Loaners is turned off.' in r.data
    assert client.get('/loaner_checkinout').status_code == 404
    save(client)  # back on
    assert client.get('/admin/loaners').status_code == 200


def test_dependency_blocks_child_module(client):
    save(client, 'tickets')
    r = client.get('/admin/automations')
    assert r.status_code == 404 and b'needs Tickets' in r.data


def test_device_page_sections_follow_switches(client, make):
    dev = make.device(holder=make.person())
    page = lambda: client.get(f'/admin/assets/{dev.asset_tag}/assign').get_data(as_text=True)  # noqa: E731
    assert 'Incidents / Damage Reports' in page() and 'Print Label' in page()
    save(client, 'incidents', 'labels')
    assert 'Incidents / Damage Reports' not in page() and 'Print Label' not in page()


def test_guardian_notice_refused_when_off(client, make, sent_emails):
    kid = make.person(guardian_email='parent@example.com')
    dev = make.device(holder=kid)
    save(client, 'guardian_notices')
    client.post(f'/admin/assets/{dev.asset_tag}/incidents', data={'description': 'x', 'notify_guardian': 'on'})
    assert sent_emails == []


def test_dashboard_charts_switch(client):
    assert 'viz-card' in client.get('/admin').get_data(as_text=True)
    save(client, 'dashboard_charts')
    assert 'viz-card' not in client.get('/admin').get_data(as_text=True)


def test_integration_setup_reachable_until_switched_off(client):
    # Google isn't configured in tests: its setup page must still open while switched on
    assert client.get('/admin/google_setup').status_code == 200
    save(client, 'google')
    assert client.get('/admin/google_setup').status_code == 404


def test_kiosk_cookie_ignored_when_kiosk_off(client, anon_client, make):
    site = make.site()
    client.post('/admin/kiosk/enable', data={'label': 'Library', 'site_id': str(site.id)})
    anon_client.set_cookie('kiosk_token', A.KioskDevice.query.one().token)
    assert anon_client.get('/checkin').status_code == 200
    save(client, 'kiosk')
    assert anon_client.get('/checkin').status_code == 302  # back to login: the kiosk is no longer trusted


def test_anonymous_users_still_go_to_login(anon_client, client):
    save(client, 'loaners')
    r = anon_client.get('/admin/loaners')
    assert r.status_code == 302 and '/admin/login' in r.headers['Location']


def test_settings_hub_lists_features_and_hides_off_modules(client):
    hub = client.get('/admin/settings').get_data(as_text=True)
    assert 'Features' in hub and 'Kiosk Devices' in hub
    save(client, 'kiosk')
    assert 'Kiosk Devices' not in client.get('/admin/settings').get_data(as_text=True)
