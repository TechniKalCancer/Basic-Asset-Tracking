"""One-off (2026-10-08): set Laptop / Desktop on devices typed "Other" that are
Windows/Mac computers in Active Directory, so the Workgroup view (laptops and
desktops not in AD) means something.

FoxDesk has no model for these devices, so the type comes from where the
computer sits in AD:
  - an OU named for laptops ("...LTs", "PressboxLT", "laptop", "notebook") -> laptop
  - a Mac anywhere else -> left as Other (the OU doesn't say MacBook or iMac)
  - any other Windows computer -> desktop
Devices are matched the same way the AD sync matches them: the computer's
serialNumber attribute, else its name (PCs here are named by service tag).
Only devices currently typed "other" are touched.

Input is an LDIF export of AD computers (objectGUID name operatingSystem
serialNumber), read through Kerberos because LDAPS wasn't on the DCs yet:
    ldapsearch -Y GSSAPI ... "(objectCategory=computer)" name operatingSystem serialNumber > computers.ldif

Run inside the web container (dry run unless --apply):
    python maintenance/2026-10-08_set_computer_types_from_ad.py /tmp/adsnap/computers.ldif [--apply]
"""
import base64
import re
import sys
from collections import Counter

sys.path.insert(0, '.')

from foxdesk import app, db  # noqa: E402
from foxdesk.models import ActivityLog  # noqa: E402
from foxdesk.services.identities import find_registry_row  # noqa: E402

ACTOR = 'Data cleanup (2026-10-08)'
# "LT"/"LTs" as its own word or the end of one ("C_STAFFf_LTs", "PressboxLT"),
# case-sensitive so "VAULT" or "Default" don't count.
LAPTOP_OU = re.compile(r'(?:^|(?<=[_\s])|(?<=[a-z]))LTs?(?=$|[_\s,])|(?i:laptop|notebook)')


def read_ldif(path):
    entries, dn, attrs = [], None, None
    for line in open(path, 'rb'):
        line = line.rstrip(b'\n')
        if not line:
            if dn:
                entries.append((dn, attrs))
                dn = None
            continue
        if line.startswith(b'#'):
            continue
        key, _, rest = line.partition(b':')
        value = base64.b64decode(rest[1:].strip()) if rest.startswith(b':') else rest.strip()
        if key.lower() == b'dn':
            dn, attrs = value.decode('utf-8', 'replace'), {}
        else:
            attrs.setdefault(key.decode().lower(), value.decode('utf-8', 'replace'))
    if dn:
        entries.append((dn, attrs))
    return entries


def ou_path(dn):
    return ','.join(p for p in re.split(r'(?<!\\),', dn)[1:] if not p.upper().startswith('DC='))


def classify(dn, os_name):
    path = ou_path(dn)
    if any(LAPTOP_OU.search(rdn.split('=', 1)[-1]) for rdn in path.split(',')):
        return 'laptop', 'laptop OU'
    if 'mac' in (os_name or '').lower():
        return None, 'Mac, form factor unknown'
    return 'desktop', 'other Windows OU'


def main(path, apply):
    changes, skipped, by_ou = [], Counter(), Counter()
    seen = set()
    for dn, attrs in read_ldif(path):
        name = attrs.get('name')
        row = find_registry_row(serial_number=attrs.get('serialnumber')) or find_registry_row(serial_number=name)
        if not row:
            skipped['not in FoxDesk'] += 1
            continue
        if row.id in seen:
            continue
        seen.add(row.id)
        if row.device_type != 'other':
            skipped[f'already {row.device_type}'] += 1
            continue
        new_type, why = classify(dn, attrs.get('operatingsystem'))
        by_ou[(ou_path(dn) or '(domain root)', new_type or 'leave Other')] += 1
        if not new_type:
            skipped[why] += 1
            continue
        changes.append((row, new_type, why, ou_path(dn)))

    print(f'{len(changes)} devices to retype: {dict(Counter(t for _, t, _, _ in changes))}')
    print('not changed:', dict(skipped))
    print('\nby OU:')
    for (ou, result), n in sorted(by_ou.items()):
        print(f'  {n:4}  {result:12} {ou}')
    if not apply:
        print('\nDry run. Re-run with --apply to save.')
        return
    for row, new_type, why, ou in changes:
        row.device_type = new_type
        db.session.add(ActivityLog(actor_type='system', actor_label=ACTOR, action='device_edit', site_id=row.site_id,
                                   summary=f'{row.asset_tag}: type Other → {new_type.capitalize()} (from AD: {ou})'))
    db.session.commit()
    print(f'\nSaved {len(changes)} changes.')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    with app.app_context():
        main(sys.argv[1], '--apply' in sys.argv[2:])
