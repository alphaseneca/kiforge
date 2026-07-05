# KiForge — PCM publishing guide

KiForge uses **KiCad PCM schema v2** (KiCad 10+). This document describes how to cut a release and publish to PCM **without** relying on `main` branch artifacts or auto-commits.

## How users install KiForge

End users install **only from PCM releases** — there is no supported “install from `main`” path:

| Method | What they get |
| --- | --- |
| PCM custom repo URL | Latest release zip via `releases/latest/download/repository.json` |
| PCM → Install from file | A tagged zip from [GitHub Releases](https://github.com/alphaseneca/kiforge/releases) |
| Official KiCad catalog | Same tagged zips, after GitLab metadata MR is merged |

Each release zip pins `KIFORGE_ACTION_REF` to `alphaseneca/kiforge@vX.Y.Z`. When they generate CD workflows (`templates/github-release.yml`, `templates/gitea-release.yml`), the **Run KiForge** step in their project uses that same tag — not `@main`.

Repo-root `kiforge.py` still says `@main` for **contributors** testing `--generate-cd` from a git clone. That is not shipped to PCM users.

## Source of truth

| What | Where it lives |
| --- | --- |
| Plugin zip + hashes | GitHub Release for tag `vX.Y.Z` |
| Repository `metadata.json` (all versions, `download_*` fields) | **`dist/metadata.json`** on each GitHub Release tag (CI-built) |
| `packages.json`, `repository.json`, `resources.zip` | **Release assets** on that tag |
| Zip-embedded `metadata.json` | Inside the plugin zip only (single version, no `download_*`) |
| Composite action ref in generated CD YAML | Pinned to `alphaseneca/kiforge@vX.Y.Z` inside each release zip |

The tagged GitHub Release is the stable snapshot. `main` may move ahead; do not use raw files from `main` for PCM.

## Prerequisites (one-time)

- [ ] `metadata.json` in git has static PCM fields only (`versions: []` — CI appends release rows).
- [ ] `plugins/icon.png` and `resources/icon.png` are committed.
- [ ] `LICENSE` is MIT (GPL-compatible).
- [ ] Issue tracker: GitHub Issues on this repository.

See also `schemas/README.md` and [KiCad addon publishing docs](https://dev-docs.kicad.org/en/addons/).

## Cut a release (every version)

### 1. Pre-flight

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

### 2. Tag the commit you want to ship

```bash
git tag -a vX.Y.Z -m "KiForge vX.Y.Z"
git push origin vX.Y.Z
```

Pushing the tag starts `.github/workflows/release.yml`, which:

1. Runs tests  
2. Builds `dist/com.github.alphaseneca.kiforge-vX.Y.Z.zip`  
3. Chains version history from the **previous** release’s `metadata.json`
4. Pins `KIFORGE_ACTION_REF` to `alphaseneca/kiforge@vX.Y.Z` inside the packaged plugin (`plugins/kiforge.py`). When users generate CD workflows from Studio or CLI, that value replaces `{{KIFORGE_ACTION_REF}}` in `templates/github-release.yml` and `templates/gitea-release.yml` — i.e. the **Run KiForge** composite action step in *their* project’s `.github/workflows/release.yml` / `.gitea/workflows/release.yml`.
5. Uploads release assets for **this tag only**:
   - `com.github.alphaseneca.kiforge-vX.Y.Z.zip`
   - `metadata.json`
   - `packages.json`
   - `repository.json`
   - `resources.zip`

Nothing is committed back to `main`.

### 3. Verify the release

Open the GitHub Release page for `vX.Y.Z` and confirm all five assets are present.

Optional local check (after CI finishes, download assets or run the same build):

```bash
python package_plugin.py --version vX.Y.Z
python -c "from package_plugin import verify_release_pcm_artifacts; verify_release_pcm_artifacts('vX.Y.Z')"
```

### 4. Install from the release zip (smoke test)

KiCad → **Plugin and Content Manager** → **Install from file…** → select the zip from the release page.

---

## Path A — Custom PCM repository (GitHub)

Users who want rolling updates add:

```text
https://github.com/alphaseneca/kiforge/releases/latest/download/repository.json
```

Users who want a **fixed** release pin that tag:

```text
https://github.com/alphaseneca/kiforge/releases/download/vX.Y.Z/repository.json
```

Each tag’s `repository.json` points at `packages.json` and `resources.zip` on **that same tag**.

---

## Path B — Official KiCad PCM catalog (GitLab)

First submission and every update use the **`metadata.json` from the GitHub Release** for the version you are publishing — not from `main`.

### First-time submission

1. Complete a tagged release (steps above).  
2. Fork [kicad/addons/metadata](https://gitlab.com/kicad/addons/metadata).  
3. Create directory `packages/com.github.alphaseneca.kiforge/`.  
4. Download `metadata.json` from the release assets for `vX.Y.Z`.  
5. Copy it to `packages/com.github.alphaseneca.kiforge/metadata.json`.  
6. Optional: add `packages/com.github.alphaseneca.kiforge/icon.png` (copy from `resources/icon.png`).  
7. Open a merge request to `kicad/addons/metadata` → `main`.  
8. Wait for KiCad team review.

Do **not** open an MR to [kicad/addons/repository](https://gitlab.com/kicad/addons/repository) — it is updated automatically from metadata.

### Later updates

1. Tag and push `vX.Y.Z` → wait for release assets.  
2. Download `metadata.json` from **that** release.  
3. Update `packages/com.github.alphaseneca.kiforge/metadata.json` in your GitLab fork.  
4. Open MR: “Update com.github.alphaseneca.kiforge to vX.Y.Z”.

Version history is chained automatically at build time from the previous release’s `metadata.json`.

---

## CD workflow action ref (downstream projects)

When a user installs plugin **vX.Y.Z** and generates `.github/workflows/release.yml` or `.gitea/workflows/release.yml`, KiForge substitutes:

```yaml
uses: alphaseneca/kiforge@vX.Y.Z          # GitHub
uses: https://github.com/alphaseneca/kiforge@vX.Y.Z   # Gitea
```

That is the same composite action as this repository’s `action.yml` (Docker + `kiforge.py`), checked out at the **matching release tag**. There is no separate “dev release” for PCM — only tagged GitHub Releases.

---

## Troubleshooting

| Problem | Check |
| --- | --- |
| PCM hash mismatch | User must install from a **release** zip or use `releases/latest/download/repository.json`, not `main` branch files |
| Missing version in official catalog MR | Use `metadata.json` from the **release assets**, not the zip-embedded copy |
| First release, no prior metadata | Build uses committed `metadata.json` as the base |
| Icons missing | `plugins/icon.png` and `resources/icon.png` must exist before tagging |

---

## Related files

| File | Role |
| --- | --- |
| `metadata.json` | Static package manifest in git (`versions: []`) |
| `package_plugin.py` | Builds zip, release metadata, PCM repo files |
| `.github/workflows/release.yml` | Tag-triggered release pipeline |
| `schemas/pcm.v2.schema.json` | Vendored PCM v2 schema |
