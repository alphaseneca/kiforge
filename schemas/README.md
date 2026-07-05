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
| **Package manifest** | `metadata.json` | Static PCM v2 fields only (`versions` must be `[]` in git). |
| **Build** | `package_plugin.py` / release CI | Appends version rows with `download_*` to `dist/metadata.json` (chains from prior GitHub Release). |
| **Repository index** | `dist/packages.json` | Wraps the manifest in a `PackageArray` with `$schema` v2. |
| **Repository pointer** | `dist/repository.json` | PCM v2 `Repository` with `schema_version: 2`. |
| **Install zip** | `metadata.json` inside the zip | Same manifest fields but **one** `versions[]` row **without** `download_url`, `download_sha256`, or `download_size` (KiCad official submission rule). |

If `metadata.json` is missing required v2 fields (e.g. `tags`), or lists any `versions[]` row in git, the build **fails** instead of silently filling defaults.

### Local dev builds

Unversioned `python package_plugin.py` inserts version `0.0.0` with `status: testing` at pack time only. Released semver rows are appended in CI to `dist/metadata.json`, chaining from the previous GitHub Release asset.

## Refreshing the bundled schema

```bash
curl -L -o schemas/pcm.v2.schema.json \
  https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/schemas/pcm.v2.schema.json
```

Then run `python package_plugin.py` and `python -m unittest tests.test_package`.
