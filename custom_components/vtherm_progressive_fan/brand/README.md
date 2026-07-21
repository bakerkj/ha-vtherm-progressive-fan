# Brand assets

`icon.svg` is the source of truth. Re-render after editing:

```sh
cairosvg icon.svg -o icon.png -W 256 -H 256
```

HA and HACS both read `icon.png` from this directory, so no PR to
`home-assistant/brands` is needed.

The guard ring is load-bearing: without it the five swept blades read as a
flower rather than a fan.
