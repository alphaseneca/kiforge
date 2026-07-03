# KiCad PCM JSON schemas

KiForge targets **KiCad 10+** and uses **PCM schema v2** for plugin metadata and custom repository files.

## Files

| File | Purpose |
| --- | --- |
| `pcm.v2.schema.json` | Official KiCad PCM schema v2 (vendored for offline reference) |

## Upstream source

https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/schemas/pcm.v2.schema.json

Canonical URI in generated JSON:

https://go.kicad.org/pcm/schemas/v2

## How KiForge uses PCM v2 (no runtime patching)

Responsibilities are split explicitly:

| Layer | File / output | Who owns the data |
| --- | --- | --- |
| **Package manifest** | `metadata.json` | You edit static PCM v2 fields: `$schema`, `name`, `tags`, `author`, `maintainer`, `resources`, `versions[].platforms`, etc. |
| **Build** | `package_plugin.py` | Only writes **build-derived** fields for the version being packaged: `download_url`, `download_sha256`, `download_size`, `install_size`, and `status` (`testing` for local `0.0.0`). |
| **Repository index** | `dist/packages.json` | Wraps the manifest in a `PackageArray` with `$schema` v2. |
| **Repository pointer** | `dist/repository.json` | PCM v2 `Repository` with `schema_version: 2`. |
| **Install zip** | `metadata.json` inside the zip | Same manifest fields but **one** `versions[]` row **without** `download_url`, `download_sha256`, or `download_size` (KiCad official submission rule). |

If `metadata.json` is missing required v2 fields (e.g. `tags` or `platforms` on a version row), the build **fails** instead of silently filling defaults.

### Local dev builds

Unversioned `python package_plugin.py` inserts/updates version `0.0.0` with `status: testing`. Released semver rows stay in `metadata.json` for history; only the row for the built version gets fresh download hashes.

## Refreshing the bundled schema

```bash
curl -L -o schemas/pcm.v2.schema.json \
  https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/schemas/pcm.v2.schema.json
```

Then run `python package_plugin.py` and `python -m unittest tests.test_package`.
