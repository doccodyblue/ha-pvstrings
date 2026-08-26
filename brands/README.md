# Artwork source

`icon.svg` is the source the shipped icons are rendered from. The rendered
PNGs live in `custom_components/pvstrings/brand/` — that is the path Home
Assistant serves them from (2026.3 and newer), so the integration shows its
own icon instead of the grey placeholder.

Sizes are fixed by the Home Assistant brand spec: `icon.png` 256×256,
`icon@2x.png` 512×512, square, transparent background.
