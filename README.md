# Daisey Cutter

Inkscape extension that punches a hole through every object beneath a selected shape.

Normally, **Path → Difference** only works on the current selection: you pick the cutter plus **one** path beneath it, run Difference, then repeat for the next path — duplicating the cutter each time. On a complex design with many shapes under one cutter, that is a lot of manual work.

Daisey Cutter does that loop for you. Pick one object as the **cutter**; the extension subtracts its shape from every object below it, leaving a transparent hole in the cutter’s exact outline. Underlying objects keep their own fill, stroke, and style. The cutter is removed afterward (or kept, if you prefer).

## Install

Copy the extension files into your Inkscape extensions directory:

```bash
# Linux
cp daisey_cutter.py daisey_cutter.inx ~/.config/inkscape/extensions/

# macOS
cp daisey_cutter.py daisey_cutter.inx ~/Library/Application\ Support/org.inkscape.Inkscape/config/inkscape/extensions/

# Windows (adjust username as needed)
copy daisey_cutter.py daisey_cutter.inx %APPDATA%\inkscape\extensions\
```

Restart Inkscape after installing.

## Usage

1. Stack shapes so the cutter sits **above** the objects you want to cut (higher in z-order / later in document order).
2. Select **exactly one** object — the cutter. It must be a single shape or path, not a group.
3. Run **Extensions → Modify Path → Daisey Cutter**.
4. Each overlapping object below the cutter gets a hole; styles are preserved.

### Options

| Option | Default | Description |
|--------|---------|-------------|
| Keep the cutter object afterwards | off | Leave the cutter in place after cutting |
| Only cut objects overlapping the cutter | on | Skip shapes whose bounds don’t intersect the cutter |

## Requirements

- Inkscape 1.2+ (uses `inkex` and headless `path-difference` actions)
- Cutter must be a single path/shape (use **Path → Union** or **Combine** first if it’s a group)

## Limitations

- Groups, images, and clones are not cut directly (children of groups that are normal shapes are fine)
- Objects inside `defs`, clip paths, masks, etc. are skipped
- Boolean ops run via a headless Inkscape process, so Inkscape must be available on `PATH`
