"""Avery sheet layouts and the Code 128 barcode encoder."""
from collections import OrderedDict
from markupsafe import Markup, escape


# Positions in inches, from Avery's published templates. Printing must be at
# "Actual size"/100% with no extra margins — called out on the page itself.
AVERY_TEMPLATES = OrderedDict([
    ('5160', {'label': 'Avery 5160 / 8160 — 30 per sheet (1" × 2⅝")', 'cols': 3, 'rows': 10,
              'width': 2.625, 'height': 1.0, 'top': 0.5, 'left': 0.1875, 'pitch_x': 2.75, 'pitch_y': 1.0}),
    ('5163', {'label': 'Avery 5163 / 8163 — 10 per sheet (2" × 4")', 'cols': 2, 'rows': 5,
              'width': 4.0, 'height': 2.0, 'top': 0.5, 'left': 0.15625, 'pitch_x': 4.1875, 'pitch_y': 2.0}),
    ('5167', {'label': 'Avery 5167 / 8167 — 80 per sheet (½" × 1¾", tag + barcode only)', 'cols': 4, 'rows': 20,
              'width': 1.75, 'height': 0.5, 'top': 0.5, 'left': 0.3, 'pitch_x': 2.05, 'pitch_y': 0.5}),
])


# Bar/space module widths for Code 128 symbol values 0-106 (106 = stop, 13 modules).
_CODE128_PATTERNS = (
    '212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 '
    '221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 '
    '221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 '
    '212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 '
    '231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 '
    '231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 '
    '314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 '
    '112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 '
    '111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 '
    '214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 '
    '114131 311141 411131 211412 211214 211232 2331112'
).split()


_CODE128_START_B, _CODE128_START_C, _CODE128_STOP = 104, 105, 106


def _code128_values(text):
    """Symbol values (start + data + checksum + stop). All-digit, even-length
    values use Code C (two digits per symbol — half the width, which matters
    on a ½"-tall 5167 label); anything else uses Code B (printable ASCII)."""
    if len(text) >= 4 and len(text) % 2 == 0 and text.isdigit():
        values = [_CODE128_START_C] + [int(text[i:i + 2]) for i in range(0, len(text), 2)]
    else:
        if any(not (32 <= ord(ch) <= 126) for ch in text):
            raise ValueError(f'Can\'t barcode "{text}" — only plain printable characters are supported.')
        values = [_CODE128_START_B] + [ord(ch) - 32 for ch in text]
    checksum = (values[0] + sum(i * v for i, v in enumerate(values[1:], start=1))) % 103
    return values + [checksum, _CODE128_STOP]


def code128_svg(text, quiet_zone=10):
    """Returns an inline <svg> for `text` as a Code 128 barcode. The viewBox is
    in barcode modules and preserveAspectRatio="none", so CSS sizes it to
    whatever box the label gives it without blurring the bars."""
    x = quiet_zone
    bars = []
    for value in _code128_values(text):
        for i, width in enumerate(_CODE128_PATTERNS[value]):
            width = int(width)
            if i % 2 == 0:  # even positions are bars, odd are spaces
                bars.append(f'<rect x="{x}" y="0" width="{width}" height="1"/>')
            x += width
    total = x + quiet_zone
    return Markup(f'<svg class="barcode" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} 1" '
                  f'preserveAspectRatio="none" shape-rendering="crispEdges" role="img" '
                  f'aria-label="Barcode {escape(text)}">{"".join(bars)}</svg>')
