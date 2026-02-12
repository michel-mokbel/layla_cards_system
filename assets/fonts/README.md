# Fonts (Arabic support)

To render Arabic text correctly in the generated PDF, you must provide an Arabic-capable TTF font.

1) Download a font such as **Noto Naskh Arabic** or **Amiri** (TTF file).
2) Put the `.ttf` file in this folder: `assets/fonts/`
3) In the app, select that font (or it will auto-pick the first TTF found).

Why: ReportLab’s default fonts don’t include Arabic glyphs, so Arabic text appears as empty squares/boxes without a proper TTF.

