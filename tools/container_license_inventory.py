"""Collect license evidence from the exact candidate container, without networking.

Run inside the image: python container_license_inventory.py /report
The output is an inventory for review, not a legal-compliance verdict.
"""

import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import subprocess
import sys


def collect(output):
    output.mkdir(parents=True, exist_ok=True)

    def evidence(source, target):
        data = source.read_bytes()
        destination = output / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return {'path': str(source), 'evidence': str(target),
                'sha256': hashlib.sha256(data).hexdigest()}

    packages = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = dist.metadata['Name']
        licenses, binaries = [], []
        for entry in dist.files or []:
            path = dist.locate_file(entry)
            if not path.is_file():
                continue
            if any(word in entry.name.lower() for word in ('license', 'copying', 'notice')):
                # Distribution paths can contain '..' for executable scripts;
                # evidence destinations must stay inside the report directory.
                safe_parts = [part for part in entry.parts if part not in ('.', '..', '/')]
                licenses.append(evidence(path, Path('python') / name / Path(*safe_parts)))
            if '.so' in entry.name:
                binaries.append({'path': str(entry),
                                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        packages.append({
            'name': name, 'version': dist.version,
            'license_expression': dist.metadata.get('License-Expression'),
            'legacy_license': dist.metadata.get('License'),
            'project_urls': dist.metadata.get_all('Project-URL', []),
            'home_page': dist.metadata.get('Home-page'),
            'license_files': licenses, 'native_binaries': binaries,
        })

    debian = subprocess.check_output([
        'dpkg-query', '-W', '-f',
        '${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n',
    ], text=True)
    (output / 'debian-packages.tsv').write_text(
        'binary_package\tversion\tsource_package\tsource_version\n' + debian)
    copyrights = []
    for path in sorted(Path('/usr/share/doc').glob('*/copyright')):
        if path.is_file():
            copyrights.append(evidence(path, Path('debian') / path.parent.name / 'copyright'))
    # Debian copyright files often refer to these shared full license texts.
    common_licenses = []
    for path in sorted(Path('/usr/share/common-licenses').glob('*')):
        if path.is_file():
            common_licenses.append(evidence(path, Path('debian/common-licenses') / path.name))

    runtime = []
    for path in sorted(Path('/usr/local/lib').glob('python*/LICENSE.txt')):
        runtime.append(evidence(path, Path('runtime') / path.parent.name / path.name))
    for filename in ['LICENSE', 'THIRD_PARTY_NOTICES.md', 'web/fonts/LICENSE.txt']:
        path = Path('/app') / filename
        runtime.append(evidence(path, Path('application') / filename))

    locked = dict(line.split('==', 1) for line in Path('/app/requirements.lock').read_text().splitlines()
                  if line.strip() and not line.startswith('#'))
    installed = {p['name'].lower(): p for p in packages}
    missing = [name for name, version in locked.items()
               if name.lower() not in installed or installed[name.lower()]['version'] != version
               or not installed[name.lower()]['license_files']]
    report = {'python_version': platform.python_version(), 'packages': packages,
              'debian_copyrights': copyrights, 'debian_common_licenses': common_licenses,
              'runtime_and_application': runtime,
              'lock_or_license_failures': missing,
              'source_archives_collected': False}
    (output / 'inventory.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'REVIEW.md').write_text(
        '# Container license evidence\n\n'
        f'- Python distributions inspected: {len(packages)}\n'
        f'- Debian copyright documents collected: {len(copyrights)}\n'
        f'- Locked package/version/license-file failures: {len(missing)}\n\n'
        'This is evidence from the candidate image, not legal clearance.\n'
        'This inventory is captured before source collection. See the release archive\n'
        'source-manifest.json for subsequently collected sources and build recipes.\n'
        'Third-party modification and replacement rights are described in the\n'
        'application THIRD_PARTY_NOTICES.md and the respective license texts.\n'
        'Upstream URLs are identification aids, not a guarantee that distribution\n'
        'obligations are fulfilled. Review the included project license and additional permission.\n')
    if missing:
        raise SystemExit('Missing locked package/version/license evidence: ' + ', '.join(missing))
    print(f'Collected license evidence for {len(packages)} Python distributions.')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: container_license_inventory.py OUTPUT_DIRECTORY')
    collect(Path(sys.argv[1]))
