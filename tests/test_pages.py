"""Every page loads, for an empty install and a populated one, and the
navigation changes from 2026-10-07 (no Home tab, Settings hub) hold."""
import re

import pytest

from conftest import A

SKIP = ('/static', '/api', '/admin/logout', '/healthz', '/admin/branding/preview', '/branding/logo')


def simple_get_routes():
    return sorted({str(r) for r in A.app.url_map.iter_rules()
                   if 'GET' in r.methods and '<' not in str(r) and not str(r).startswith(SKIP)})


def assert_loads(r, url):
    """200/302, or the deliberate "turned off / not set up" page for a
    module that isn't available on this install (e.g. the Google sign-in
    check when Google isn't connected)."""
    if r.status_code == 404:
        assert b'Open Settings' in r.data or b'Ask your FoxDesk administrator' in r.data, f'{url} -> real 404'
        return
    assert r.status_code in (200, 302), f'{url} -> {r.status_code}'


@pytest.mark.parametrize('url', simple_get_routes())
def test_page_loads_on_empty_install(client, url):
    assert_loads(client.get(url), url)


def test_pages_load_with_data(client, make):
    site = make.site()
    kid = make.person(site=site)
    dev = make.device(holder=kid, site=site)
    make.ticket_category()
    detail_urls = [f'/admin/assets/{dev.asset_tag}/assign', f'/admin/people/{kid.id}/edit',
                   f'/admin/people/{kid.id}/history', f'/admin/registry/{dev.asset_tag}/edit']
    for url in simple_get_routes() + detail_urls:
        assert_loads(client.get(url), url)


def test_admin_pages_require_login(anon_client):
    for url in ('/admin', '/admin/registry', '/admin/data_quality', '/admin/signin_mismatches', '/admin/settings'):
        r = anon_client.get(url)
        assert r.status_code == 302 and '/admin/login' in r.headers['Location'], url


def test_home_redirects_logged_in_users_to_dashboard(client):
    r = client.get('/')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin')


def test_nav_has_dashboard_tab_and_no_home_tab(client):
    body = client.get('/admin').get_data(as_text=True)
    tabs = re.findall(r'class="nav-tab[^"]*"[^>]*>(?:<span[^>]*></span>)?([^<]+)<', body)
    labels = [t.strip() for t in tabs]
    assert 'Dashboard' in labels and 'Home' not in labels


def test_settings_hub_lists_areas(client):
    body = client.get('/admin/settings').get_data(as_text=True)
    for area in ('Users &amp; Permissions', 'Sites', 'Email', 'Branding', 'Activity Log'):
        assert area in body


def test_no_emoji_in_rendered_pages(client, make):
    emoji = re.compile('[\U0001F000-\U0001FAFF☀-⛿⬀-⯿️]')
    make.device(holder=make.person())
    for url in ('/admin', '/admin/registry', '/admin/people', '/admin/settings', '/report_problem'):
        found = set(emoji.findall(client.get(url).get_data(as_text=True))) - {'☰'}  # ☰ is the text fallback for the menu icon
        assert not found, f'{url}: {found}'


def test_static_files_served_from_project_root(anon_client):
    # foxdesk/core.py points Flask's root at the project, not the package dir
    for path in ('/static/js/charts.js', '/static/js/camera_scan.js', '/static/icons/devices.svg'):
        assert anon_client.get(path).status_code == 200, path
