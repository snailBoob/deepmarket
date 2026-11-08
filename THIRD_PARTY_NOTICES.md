# Third-party components

The Deepmarket license applies to project-owned material, not to the components
below. Preserve their license texts, notices and applicable source-code rights.

## Fonts

IBM Plex Mono is copyright IBM Corp., distributed under the SIL Open Font License
1.1, with the reserved font name "Plex". The license is included in
[`web/fonts/LICENSE.txt`](web/fonts/LICENSE.txt) and in the container's font directory.

## Python dependencies

Exact versions are defined in [`requirements.lock`](requirements.lock).
The following license metadata was inspected for those versions:

| Distribution | License metadata |
| --- | --- |
| aiohappyeyeballs | PSF-2.0 |
| aiohttp | Apache-2.0 AND MIT |
| aiosignal | Apache-2.0 |
| attrs | MIT |
| frozenlist | Apache-2.0 |
| idna | BSD-3-Clause |
| multidict | Apache-2.0 |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| orjson | MPL-2.0 AND (Apache-2.0 OR MIT) |
| propcache | Apache-2.0 |
| yarl | Apache-2.0 |

Installed distributions retain their license and NOTICE files under their
`.dist-info/` directories. Copies are also provided in the release's
`third-party-sources-v1.0.0.tar.gz` archive, under `bundle/license-evidence/`.

The Linux/amd64 NumPy wheel includes OpenBLAS/LAPACK, libgfortran (GPL with
the GCC runtime exception) and libquadmath (LGPL). orjson includes MPL-covered
material. Their matching sources, patches and build recipes are included in the
same archive. `source-manifest.json` identifies the exact component versions and
checksums. The archive includes the orjson vendored Rust sources and the GCC
source RPM for the AlmaLinux runtime libraries bundled with NumPy.

These components remain under their respective licenses, including their rights
to modify, redistribute and replace the components. Deepmarket's noncommercial
restriction does not apply to them. Reverse engineering to debug modifications
to LGPL-covered components is permitted. The container is not locked: you can
replace shared libraries or rebuild a derived image using modified dependencies.

## Container base

The image includes Python and Debian packages with their own licenses, retained
under `/usr/share/doc/*/copyright`, `/usr/share/common-licenses/` and the Python
installation. Matching source packages and the Python container build recipe are
included in the source archive. Debian source packages contain their upstream
sources, distribution patches and packaging/build instructions.

Download the matching source archive alongside each Docker image from the
[GitHub releases](https://github.com/snailBoob/deepmarket/releases). No account
or payment is required. Retain the matching sources and notices when
redistributing an image. A locally rebuilt image may have different dependencies;
the supplied archive describes the release image, not arbitrary later rebuilds.
