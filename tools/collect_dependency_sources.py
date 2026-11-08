"""Download exact third-party source archives for a container release.

Runs in a disposable candidate container; never extracts or executes sources.
Download verification is not a verdict that corresponding source is complete.
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import quote
from urllib.request import urlopen


# Reviewed NumPy 2.5.4 Linux x86_64 wheel. A new binary hash must be reviewed
# again rather than silently inheriting these source mappings.
# scipy-openblas64 0.3.34.106.0's auditwheel SBOM identifies the AlmaLinux
# runtime package version; its annotated release tag pins the recipe and
# OpenBLAS submodule below. Parent-wheel .text/.rodata and all other sections
# except dynamic linking metadata match the NumPy-bundled binaries.
NUMPY_NATIVE_SOURCES = {
    'a6a368328d02a5d8a149746f13f6268f8cc9192df5c18777b25c3c9b5ef52160': 'gcc',
    '6bc3069003caec1f075be3c51ae8355332f081fa9465910c4b5724e7fe7d8ad4': 'gcc',
    '432e1c1739731468ba305771af96773939e43016643e2d630f58cb45b6670e96': 'openblas',
}
NATIVE_ARCHIVES = [
    ('gcc', 'gcc-8.5.0-28.el8_10.alma.1.src.rpm',
     'https://repo.almalinux.org/vault/8.10/BaseOS/Source/Packages/gcc-8.5.0-28.el8_10.alma.1.src.rpm',
     'c94dbbd2cd6d5d01a5a6822ea18f8ce34942c25a80e17b4682d547d5e652dbd0'),
    ('openblas', 'OpenBLAS-446c436e10450c348169808f1a6b3fae0925c9f7.tar.gz',
     'https://codeload.github.com/OpenMathLib/OpenBLAS/tar.gz/446c436e10450c348169808f1a6b3fae0925c9f7',
     'ddecf3872f66f556aed35dabe9e04e663f014b05052b989288f28fc9a883afe0'),
    ('recipe', 'openblas-libs-78fc0eaf6de71d92fd98f74f7e5bfa356a2f83e3.tar.gz',
     'https://codeload.github.com/MacPython/openblas-libs/tar.gz/78fc0eaf6de71d92fd98f74f7e5bfa356a2f83e3',
     'ce4197b8d3c006370d3ee0f4e2d9d4940480b649a62fa209a405d419666cfb6b'),
    ('provenance', 'scipy_openblas64-0.3.34.106.0-py3-none-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl',
     'https://files.pythonhosted.org/packages/3a/87/127a5870cf8cb84b275046084ec93d9e218616fafff257ffb6e04b76499b/scipy_openblas64-0.3.34.106.0-py3-none-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl',
     'e3d0667352f91ad851141a99eb326a6bbb4142db898e4f68bff8665247f0e951'),
]


def native_source_mapping(package, binary):
    if package['name'].lower() == 'numpy' and package['version'] == '2.5.4':
        return NUMPY_NATIVE_SOURCES.get(binary['sha256'])
    return None


def safe_filename(value):
    if not value or value in ('.', '..') or Path(value).name != value or '\\' in value:
        raise ValueError(f'Unsafe filename: {value!r}')
    return value


def verify_file(path, digest, size=None):
    if size is not None and path.stat().st_size != size:
        raise ValueError(f'Size mismatch: {path.name}')
    actual = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            actual.update(chunk)
    if actual.hexdigest() != digest:
        raise ValueError(f'SHA-256 mismatch: {path.name}')


def download(url, path, digest, size=None):
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError(f'Missing or invalid SHA-256 for {path.name}')
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_name(path.name + '.part')
        try:
            with urlopen(url, timeout=90) as response, temporary.open('wb') as stream:
                shutil.copyfileobj(response, stream)
            verify_file(temporary, digest, size)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    verify_file(path, digest, size)
    return {'url': url, 'sha256': digest, 'size': path.stat().st_size}


def parse_deb822(text):
    records, current, key = [], {}, None
    for line in text.splitlines() + ['']:
        if not line:
            if current:
                records.append(current)
            current, key = {}, None
        elif line[0].isspace() and key:
            current[key] += '\n' + line.strip()
        else:
            key, separator, value = line.partition(':')
            if not separator:
                raise ValueError('Malformed Debian source metadata')
            current[key] = value.strip()
    return records


def exact_debian_source(text, name, version):
    matches = [record for record in parse_deb822(text)
               if record.get('Package') == name and record.get('Version') == version]
    if not matches:
        raise ValueError(f'Exact Debian source unavailable: {name}={version}')
    checksums = matches[0].get('Checksums-Sha256', '')
    files = []
    for line in checksums.splitlines():
        if not line.strip():
            continue
        digest, size, name = line.split()
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid Debian source checksum')
        files.append({'file': safe_filename(name), 'sha256': digest, 'size': int(size)})
    if not files or not any(item['file'].endswith('.dsc') for item in files):
        raise ValueError('Debian source metadata lacks a complete file list')
    return matches[0], files


def run(command, **kwargs):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=300, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{command[0]} failed ({result.returncode}): {result.stdout[-4000:]}')
    return result.stdout


def enable_source_indexes(output):
    # No host package-manager changes: this is only allowed inside a disposable
    # container, explicitly opted in by the workflow.
    if not Path('/.dockerenv').is_file() or os.environ.get('DEEP_MARKET_SOURCE_REVIEW') != '1':
        raise RuntimeError('Debian collection requires the disposable review container')
    sources = Path('/etc/apt/sources.list.d/debian.sources')
    original = sources.read_text()
    (output / 'debian.sources.original').write_text(original)
    changed, count = re.subn(r'^Types:.*$', 'Types: deb deb-src', original, flags=re.M)
    if not count:
        raise ValueError('No Debian source configuration found')
    sources.write_text(changed)
    (output / 'apt-update.log').write_text(run([
        'apt-get', '-o', 'Acquire::Retries=3', 'update']))


def collect(evidence, output):
    output.mkdir(parents=True, exist_ok=False)
    inventory = json.loads((evidence / 'licenses/inventory.json').read_text())
    image = json.loads((evidence / 'image-inspect.json').read_text())[0]
    shutil.copytree(evidence / 'licenses', output / 'license-evidence')
    shutil.copy2(evidence / 'image-inspect.json', output / 'image-inspect.json')
    if (evidence / 'build-inputs').exists():
        shutil.copytree(evidence / 'build-inputs', output / 'build-inputs')
    archives, failures, coverage_gaps, native_mappings = [], [], [], []

    for package in inventory['packages']:
        name, version = package['name'], package['version']
        label = f'PyPI {name}=={version}'
        try:
            url = f'https://pypi.org/pypi/{quote(name, safe="")}/{quote(version, safe="")}/json'
            with urlopen(url, timeout=30) as response:
                metadata = json.load(response)
            candidates = [item for item in metadata['urls'] if item['packagetype'] == 'sdist']
            if len(candidates) != 1:
                raise ValueError(f'Expected one source archive, found {len(candidates)}')
            asset = candidates[0]
            folder = output / 'python' / safe_filename(name)
            path = folder / safe_filename(asset['filename'])
            record = download(asset['url'], path, asset['digests']['sha256'], asset['size'])
            (folder / 'pypi-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
            archives.append(dict(record, component=label, file=str(path.relative_to(output))))
            print(f'Collected {label}', flush=True)
        except (OSError, ValueError, KeyError) as exc:
            failures.append({'component': label, 'reason': str(exc)})
        for binary in package['native_binaries']:
            if '.libs/' in binary['path']:
                source = native_source_mapping(package, binary)
                if source:
                    native_mappings.append({'binary': binary['path'],
                                            'binary_sha256': binary['sha256'],
                                            'source': source})
                    continue
                coverage_gaps.append({
                    'component': f'{name}: {binary["path"]}',
                    'binary_sha256': binary['sha256'],
                    'reason': 'Wheel-bundled library: exact upstream revision, patches and build recipe still need mapping; package sdist alone is not sufficient.',
                })

    if native_mappings:
        for component, filename, url, digest in NATIVE_ARCHIVES:
            try:
                path = output / 'native' / filename
                record = download(url, path, digest)
                archives.append(dict(record, component=f'NumPy bundled {component}',
                                     file=str(path.relative_to(output))))
            except (OSError, ValueError) as exc:
                failures.append({'component': f'NumPy bundled {component}', 'reason': str(exc)})

    try:
        environment = dict(value.split('=', 1) for value in image['Config']['Env'])
        version = inventory['python_version']
        if environment['PYTHON_VERSION'] != version:
            raise ValueError('CPython runtime and image source version disagree')
        path = output / 'cpython' / f'Python-{version}.tar.xz'
        url = f'https://www.python.org/ftp/python/{version}/{path.name}'
        record = download(url, path, environment['PYTHON_SHA256'])
        archives.append(dict(record, component=f'CPython {version}', file=str(path.relative_to(output))))
        if version != '3.13.18':
            raise ValueError('CPython build recipe requires review for this version')
        path = output / 'cpython' / 'Dockerfile.python'
        record = download(
            'https://raw.githubusercontent.com/docker-library/python/152874b138f0b1c4699fbdcb52b174608cfbfff5/3.13/slim-bookworm/Dockerfile',
            path, '70be02ef4e0ddfaa03548e710c5c0ee4d3295b08f813cbe165cdd8b07bb690c5')
        archives.append(dict(record, component='CPython container build recipe', file=str(path.relative_to(output))))
    except (OSError, ValueError, KeyError) as exc:
        failures.append({'component': 'CPython', 'reason': str(exc)})

    with (evidence / 'licenses/debian-packages.tsv').open() as stream:
        packages = sorted({(row['source_package'], row['source_version'])
                           for row in csv.DictReader(stream, delimiter='\t')})
    try:
        enable_source_indexes(output)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        failures.append({'component': 'Debian source indexes', 'reason': str(exc)})
    else:
        for name, version in packages:
            label = f'Debian {name}={version}'
            try:
                # Hash the version for portable directory names (Debian epochs
                # contain colons). The full version remains in the manifest.
                folder = output / 'debian' / (safe_filename(name) + '-' + hashlib.sha256(version.encode()).hexdigest()[:12])
                folder.mkdir(parents=True)
                text = run(['apt-cache', '--only-source', 'showsrc', name])
                metadata, files = exact_debian_source(text, name, version)
                (folder / 'source-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
                log = run(['apt-get', '-o', 'Acquire::Retries=3', '-o', 'APT::Sandbox::User=root',
                           '--yes', '--download-only', '--only-source', 'source', f'{name}={version}'], cwd=folder)
                (folder / 'download.log').write_text(log)
                for item in files:
                    path = folder / item['file']
                    verify_file(path, item['sha256'], item['size'])
                    archives.append(dict(item, component=label, file=str(path.relative_to(output))))
                print(f'Collected {label}', flush=True)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                failures.append({'component': label, 'reason': str(exc)})

    report = {'image_id': image['Id'], 'archives': archives, 'download_failures': failures,
              'native_source_mappings': native_mappings,
              'downloads_complete': not failures, 'coverage_gaps': coverage_gaps,
              'source_coverage_complete': not failures and not coverage_gaps}
    (output / 'source-manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(
        f'{item["sha256"]}  {item["file"]}\n' for item in sorted(archives, key=lambda item: item['file'])))
    (output / 'README.md').write_text(
        '# Third-party source archive\n\n'
        f'Candidate image: `{image["Id"]}`\n\n'
        f'Verified source files: {len(archives)}. Download failures: {len(failures)}. '
        f'Open coverage items: {len(coverage_gaps)}.\n\n'
        'See `source-manifest.json` for exact versions, checksums and unresolved items.\n'
        'Verify files with `sha256sum -c SHA256SUMS`. Sources have not been extracted or executed.\n'
        'The copied license evidence describes the image before source collection; this manifest\n'
        'records the subsequent downloads. Source coverage refers to the recorded components;\n'
        'it is not an automated legal verdict. The native mappings apply only to reviewed binary\n'
        'hashes. Debian sources include distribution patches and packaging; the GCC source RPM\n'
        'includes upstream sources, patches and its build spec. Python source distributions\n'
        'include their build definitions, including vendored Rust sources in orjson.\n\n'
        'Preserve these sources and license notices with the matching release. Third-party\n'
        'components retain their own licenses and are not subject to Deepmarket use restrictions.\n')
    print(f'Verified {len(archives)} source files; {len(failures)} download failures; '
          f'{len(coverage_gaps)} open coverage items.', flush=True)
    if failures or coverage_gaps:
        raise SystemExit('Source collection incomplete; see source-manifest.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    collect(args.evidence, args.output)
