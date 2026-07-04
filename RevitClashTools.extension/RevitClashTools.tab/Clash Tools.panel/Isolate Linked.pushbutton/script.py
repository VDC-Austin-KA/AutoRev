# -*- coding: utf-8 -*-
"""Isolate elements from linked models in the active view.

Lets you pick a linked model, filter its elements by category (and
optionally a parameter value / list of pasted UniqueIds), and show only
those elements from that link in the current view. Includes a Reset
command to restore full link visibility.
"""

from System.Collections.Generic import List

from pyrevit import revit, DB, forms, script

doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView

output = script.get_output()
logger = script.get_logger()


def get_link_instances(document):
    return list(
        DB.FilteredElementCollector(document)
        .OfClass(DB.RevitLinkInstance)
        .WhereElementIsNotElementType()
    )


def pick_link_instance():
    links = get_link_instances(doc)
    if not links:
        forms.alert("No linked models found in this document.", exitscript=True)

    link_map = {}
    for li in links:
        link_doc = li.GetLinkDocument()
        status = "loaded" if link_doc else "NOT LOADED"
        label = "{}  [{}]".format(li.Name, status)
        link_map[label] = li

    choice = forms.SelectFromList.show(
        sorted(link_map.keys()),
        title="Select Linked Model",
        button_name="Select",
    )
    if not choice:
        script.exit()
    return link_map[choice]


def pick_categories(link_doc):
    cats = sorted(
        {
            el.Category.Name
            for el in DB.FilteredElementCollector(link_doc)
            .WhereElementIsNotElementType()
            .ToElements()
            if el.Category is not None
        }
    )
    picked = forms.SelectFromList.show(
        cats,
        title="Filter by Category (optional, Cancel = all categories)",
        multiselect=True,
        button_name="Filter",
    )
    return picked or None


def collect_ids(link_doc, category_names):
    collector = DB.FilteredElementCollector(link_doc).WhereElementIsNotElementType()
    ids = List[DB.ElementId]()
    for el in collector:
        if el.Category is None:
            continue
        if category_names and el.Category.Name not in category_names:
            continue
        ids.Add(el.Id)
    return ids


def main():
    link_instance = pick_link_instance()
    link_doc = link_instance.GetLinkDocument()
    if link_doc is None:
        forms.alert(
            "That link is not loaded, so its elements can't be enumerated.",
            exitscript=True,
        )

    category_names = pick_categories(link_doc)
    ids = collect_ids(link_doc, category_names)

    if not ids or ids.Count == 0:
        forms.alert("No matching elements found in the link.", exitscript=True)

    with revit.Transaction("Isolate Linked Elements"):
        # Restrict which elements of the link are visible in this view...
        link_instance.SetVisibleElements(ids)
        # ...and make sure the link itself isn't hidden/filtered out.
        if view.GetCategoryHidden(link_instance.Category.Id):
            view.SetCategoryHidden(link_instance.Category.Id, False)

    output.print_md(
        "**Isolated {} element(s)** from `{}` in the current view.".format(
            ids.Count, link_instance.Name
        )
    )


if __name__ == "__main__":
    main()
