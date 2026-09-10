# Bundled Studio icons

[Material Symbols](https://fonts.google.com/icons) (Apache License 2.0), used for
the Studio notebook tabs and message-dialog severity glyphs.

**These are bundled deliberately.** They used to be fetched from
`fonts.gstatic.com` on first render and cached. That failed outright on macOS:
KiCad's bundled Python is a python.org framework build with no default CA store,
so every HTTPS request raises `CERTIFICATE_VERIFY_FAILED` and each icon silently
rendered blank — while the same code worked on Windows, where Python uses the OS
certificate store. A plugin should not need the network to draw its own UI.

Filenames map to `kiforge.TAB_ICON_CDN` keys, not to Material Symbol names, so
`kiforge.read_bundled_tab_icon_svg()` can find them by tab/message key:

| File | Material Symbol |
|---|---|
| `export.svg` | `file_download` |
| `advanced.svg` | `tune` |
| `releases.svg` | `label` |
| `msg_success.svg` | `check_circle` |
| `msg_error.svg` | `error` |
| `msg_warning.svg` | `warning` |
| `msg_cancelled.svg` | `cancel` |
| `msg_info.svg` | `info` |
| `msg_question.svg` | `help` |

Edit files here only; `package_plugin.py` copies them into the plugin zip at
`plugins/icons/` at build time, the same way `templates/` is handled.
