# NovaMind desktop identity

`novamind.ico` contains 16/20/24/32/40/48/64/128/256 px variants. The icon uses
the existing SVG NovaMind mark on a navy tile so it stays recognizable on both
light and dark Windows taskbars. `novamind.png` is its 256 px preview.

Both files are generated from `novamind/webui/static/nova-mark.svg` by
`packaging/build_icons.py`, using the project's existing Pillow dependency.
`NovaMind.spec` runs that generator before building, embeds the ICO into the
EXE, and bundles the icon for the native window and system tray.

To regenerate without packaging:

```powershell
.venv/Scripts/python.exe packaging/build_icons.py
```
