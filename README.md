# AutoRev

Revit automation tools, packaged as a [pyRevit](https://github.com/eirannejad/pyRevit) extension.

## RevitClashTools.extension

A pyRevit tab (**RevitClashTools**) with a **Clash Tools** panel containing two commands:

### Isolate Linked
Isolate elements from a linked model in the active view. Pick a loaded
Revit link, optionally filter by one or more categories, and the view is
narrowed (via `RevitLinkInstance.SetVisibleElements`) to just those
elements. The category filter is optional — cancel it to keep all
categories.

### Clash Viewer
Import a Navisworks Clash Detective **XML** report, resolve each clash
side to a real Revit element, then **group and color the clashed
elements by any parameter**.

**Workflow:**

1. Map each source model in the report to a loaded Revit link (or the
   host model), then **Resolve Elements**.
2. Choose a **Group by** key and click **Build Groups** — one color
   group is created per distinct value across all resolved clash
   elements, with colors auto-assigned from a distinct palette.
3. Edit any group's color or untick groups you want to skip.
4. **Apply Colors** (and **Reset View** to clear everything).

**Group by** supports: Source File, Category, Family, Type, System
Classification, System Type, System Name, Workset, Level, Clash Test, or
a **Custom Parameter** (type the parameter name — read per element via
`LookupParameter`). This effectively creates a color-coded filter over
the clashed elements for whichever parameter you choose.

**Element matching** does not depend on any single "Write Report" export
option in Navisworks. For each clash item it tries, in order:

1. A numeric element id — the `Element Id` smarttag, or a trailing
   `[12345]` / `(12345)` in the item name — resolved via
   `GetElement(ElementId(n))` in the mapped document. This is the
   reliable key for Revit-authored models.
2. A GUID property — resolved via `GetElement(uniqueId)`, then against
   the `IFC_GUID` parameter. Note Navisworks' own `<objectattribute>`
   GUID is a computed hash, **not** a Revit `UniqueId`, so this path
   mainly helps IFC-sourced models.

The parser reads the standard Navisworks `<exchange>` report layout
(`<clashtest>` → `<clashresult>` → `<clashobjects>` → `<clashobject>`,
with `<smarttag>`/`<objectattribute>` name/value children and the model
path in `<pathlink>`/`<node>`), and prefers the authored model
(`.rvt`/`.ifc`/…) over the Navisworks cache (`.nwc`) as each side's
source, so it lines up with your Revit link names.

Use **Resolve Elements** to see the match rate, and **Inspect Raw XML**
on a selected row to view that clash's exact XML if matching fails. If a
report can't be parsed at all, the tool writes a tag-structure census to
the pyRevit output window to diagnose the format.

**Known limitation:** Revit's graphic-override API supports per-element
colors for host-document elements, but only whole-link overrides for
linked elements. When a single link holds clashed elements belonging to
more than one color group, that link is colored by its **dominant**
group (the one covering the most elements) and a note lists the affected
links. Host elements always get exact per-group colors. The optional
"Isolate link to clash elems" narrows each mapped link to only its
matched clash elements. **Reset View** clears all overrides and restores
full link visibility.

## Installation

1. Copy `RevitClashTools.extension` into your pyRevit extensions folder
   (`%APPDATA%\pyRevit\Extensions`), **or** add its parent folder under
   pyRevit → Settings → Custom Extension Directories.
2. pyRevit → Reload.

## Requirements

- Revit with pyRevit installed.
- For Clash Viewer: a Navisworks Clash Detective report exported as XML
  (Clash Detective → Report → Write Report → File type: XML).
