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
Import a Navisworks Clash Detective **XML** report, map each clash side
to a loaded Revit link (or the host model), and color / isolate the
matched elements per source model.

**Element matching** does not depend on any single "Write Report" export
option in Navisworks. For each clash item it tries, in order:

1. A GUID property (Revit `Element.UniqueId`, or an IFC GUID → matched
   against the `IFC_GUID` parameter).
2. A bare numeric element-id property (e.g. "Element ID" / "Item ID").
3. The trailing `[12345]` / `(12345)` that Revit's Navisworks exporter
   bakes into every item's display name by default — so matching works
   even when no item properties were exported at all.

Use **Resolve GUIDs** to see the match rate, and **Inspect Raw XML** on a
selected row to view that clash's exact XML if matching fails.

**Known limitation:** Revit's graphic-override API supports per-element
colors for host-document elements, but only whole-link overrides for
linked elements. When elements from the same link belong to different
clashes, they share that link's color; isolation still narrows the link
to only the matched clash elements so they read as highlighted. **Reset
View** clears all overrides and restores full link visibility.

## Installation

1. Copy `RevitClashTools.extension` into your pyRevit extensions folder
   (`%APPDATA%\pyRevit\Extensions`), **or** add its parent folder under
   pyRevit → Settings → Custom Extension Directories.
2. pyRevit → Reload.

## Requirements

- Revit with pyRevit installed.
- For Clash Viewer: a Navisworks Clash Detective report exported as XML
  (Clash Detective → Report → Write Report → File type: XML).
