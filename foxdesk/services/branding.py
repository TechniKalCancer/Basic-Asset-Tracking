"""District branding: logo files and the color palette engine."""
import colorsys
import os
import re
import secrets
from flask import url_for
from foxdesk.core import BRANDING_ALLOWED_EXTENSIONS, BRANDING_UPLOAD_DIR, db
from foxdesk.models import BrandingSettings, Site
from foxdesk.services.auth import _current_site_ids


def _save_branding_logo(file_storage, prefix):
    """
    Validates and saves an uploaded logo, returning its on-disk filename (to
    store on BrandingSettings.logo_filename or Site.logo_filename) or None if
    no file was submitted. Raises ValueError on an invalid extension.

    A random suffix busts browser/CDN caching when a logo is replaced — the
    old URL (old filename) simply stops resolving rather than serving a
    stale cached image at a now-reused path.
    """
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in BRANDING_ALLOWED_EXTENSIONS:
        raise ValueError(f'Unsupported file type "{ext}". Use PNG, JPG, SVG, WebP, or ICO.')
    filename = f'{prefix}_{secrets.token_hex(4)}{ext}'
    file_storage.save(os.path.join(BRANDING_UPLOAD_DIR, filename))
    return filename


def _delete_branding_logo(filename):
    if not filename:
        return
    path = os.path.join(BRANDING_UPLOAD_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


APP_BG_HEX = '#0f1117'  # must track :root's --bg in base.html — see generate_palette()


_HEX_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f'#{r:02x}{g:02x}{b:02x}'


def _relative_luminance(rgb):
    """WCAG 2.x relative luminance (0=black, 1=white)."""
    def channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast_ratio(rgb_a, rgb_b):
    """WCAG contrast ratio, 1 (no contrast) to 21 (black vs white)."""
    l1 = _relative_luminance(rgb_a) + 0.05
    l2 = _relative_luminance(rgb_b) + 0.05
    return max(l1, l2) / min(l1, l2)


def _best_text_color(bg_hex):
    """Whichever of black/white reads better on top of bg_hex."""
    bg_rgb = _hex_to_rgb(bg_hex)
    return '#000000' if _contrast_ratio((0, 0, 0), bg_rgb) >= _contrast_ratio((255, 255, 255), bg_rgb) else '#ffffff'


def _ensure_min_contrast(hex_color, against_hex, min_ratio=4.5):
    """
    Nudges hex_color's HSL lightness toward whichever direction increases
    contrast against against_hex, stepping until min_ratio is met or the
    lightness bound (0/1) is hit. Preserves hue/saturation — a nudged brand
    red stays recognizably that red, just legible against a near-black page.
    """
    rgb = _hex_to_rgb(hex_color)
    against_rgb = _hex_to_rgb(against_hex)
    if _contrast_ratio(rgb, against_rgb) >= min_ratio:
        return hex_color

    h, l, s = colorsys.rgb_to_hls(*(c / 255.0 for c in rgb))
    direction = 1 if _relative_luminance(against_rgb) < 0.5 else -1
    result_hex = hex_color
    for _ in range(40):
        l = min(1.0, max(0.0, l + direction * 0.02))
        candidate_rgb = tuple(c * 255.0 for c in colorsys.hls_to_rgb(h, l, s))
        result_hex = _rgb_to_hex(candidate_rgb)
        if _contrast_ratio(candidate_rgb, against_rgb) >= min_ratio or l <= 0.0 or l >= 1.0:
            break
    return result_hex


def _rotated_hue(hex_color, degrees):
    rgb = _hex_to_rgb(hex_color)
    h, l, s = colorsys.rgb_to_hls(*(c / 255.0 for c in rgb))
    h = (h + degrees / 360.0) % 1.0
    return _rgb_to_hex(tuple(c * 255.0 for c in colorsys.hls_to_rgb(h, l, min(1.0, s * 0.92))))


def generate_palette(primary_hex, bg_hex=APP_BG_HEX):
    """
    Given one admin-picked brand color, derives a full contrast-safe palette
    for this app's dark theme:
      - accent: primary_hex, nudged (if needed) to >=4.5:1 against bg_hex —
        used both as a solid fill (buttons/badges) and as standalone text/
        links directly on the page background, matching how --accent is
        already used throughout the existing CSS.
      - accent_dim: a darker/desaturated variant for hover states.
      - accent_text: best(black, white) for text drawn on top of accent.
      - secondary/tertiary: +140/-140 degree hue rotations of accent (a
        split-complementary-ish spread — distinct from primary without the
        harsher clash of a true 180 degree complement), each independently
        nudged for >=4.5:1 against bg_hex, with their own best-contrast text
        color.
    Returns a dict of hex strings, all direct CSS custom-property values.
    """
    accent = _ensure_min_contrast(primary_hex, bg_hex, min_ratio=4.5)
    accent_rgb = _hex_to_rgb(accent)
    h, l, s = colorsys.rgb_to_hls(*(c / 255.0 for c in accent_rgb))

    dim_l = max(0.0, l * 0.55)
    accent_dim = _rgb_to_hex(tuple(c * 255.0 for c in colorsys.hls_to_rgb(h, dim_l, s)))

    secondary = _ensure_min_contrast(_rotated_hue(accent, 140), bg_hex, min_ratio=4.5)
    tertiary = _ensure_min_contrast(_rotated_hue(accent, -140), bg_hex, min_ratio=4.5)

    return {
        'accent': accent,
        'accent_dim': accent_dim,
        'accent_text': _best_text_color(accent),
        'secondary': secondary,
        'secondary_text': _best_text_color(secondary),
        'tertiary': tertiary,
        'tertiary_text': _best_text_color(tertiary),
    }


# Every value here is a literal match for what base.html's :root already
# hardcoded before branding existed — an unconfigured install (or a
# BrandingSettings field left blank) must look pixel-identical to today.
_DEFAULT_BRANDING = {
    'logo_url': None,
    'favicon_url': None,
    'app_name': None,
    'logo_background': None,
    'accent': '#4f7ef8', 'accent_dim': '#2a4aaa', 'accent_text': '#ffffff',
    'secondary': '#f15e56', 'secondary_text': '#000000',
    'tertiary': '#b5f156', 'tertiary_text': '#000000',
}


def _current_branding():
    """
    Resolves the logo/colors for the current request: a site-specific logo
    (from a single-site admin login or a kiosk device tied to a site) takes
    priority over the district-wide default logo; colors are always the
    single global palette (see admin_branding()/generate_palette()) since
    real schools sharing a district brand generally share one color scheme
    even when their logos differ — split further into per-site colors later
    if that stops being true.
    """
    settings = BrandingSettings.query.get(1)
    branding = dict(_DEFAULT_BRANDING)
    if settings:
        branding['app_name']       = settings.app_name or None
        branding['logo_background'] = settings.logo_background or None
        branding['accent']         = settings.primary_color or branding['accent']
        branding['accent_dim']     = settings.accent_dim_color or branding['accent_dim']
        branding['accent_text']    = settings.accent_text_color or branding['accent_text']
        branding['secondary']      = settings.secondary_color or branding['secondary']
        branding['secondary_text'] = settings.secondary_text_color or branding['secondary_text']
        branding['tertiary']       = settings.tertiary_color or branding['tertiary']
        branding['tertiary_text']  = settings.tertiary_text_color or branding['tertiary_text']

    logo_filename = settings.logo_filename if settings else None
    site_ids = _current_site_ids()
    if site_ids and len(site_ids) == 1:
        site = Site.query.get(site_ids[0])
        if site and site.logo_filename:
            logo_filename = site.logo_filename
    if logo_filename:
        branding['logo_url'] = url_for('branding_logo', filename=logo_filename)
    if settings and settings.favicon_filename:
        branding['favicon_url'] = url_for('branding_logo', filename=settings.favicon_filename)

    return branding


def _get_branding_settings():
    """Get-or-create the single BrandingSettings row (always id=1)."""
    settings = BrandingSettings.query.get(1)
    if not settings:
        settings = BrandingSettings(id=1)
        db.session.add(settings)
        db.session.commit()
    return settings
