# -*- coding: utf-8 -*-
"""Flexible parser for Navisworks Clash Detective XML reports.

The standard Navisworks "Write Report -> XML" format (root <exchange>)
nests things like this:

    <clashtests>
      <clashtest name="Test 1">
        <clashresults>
          <clashresult name="Clash1" guid="..." status="new">
            <clashobjects>
              <clashobject>
                <smarttags>
                  <smarttag><name>Element ID</name><value>123456</value></smarttag>
                  <smarttag><name>Layer</name><value>...</value></smarttag>
                </smarttags>
                <pathlink>
                  <node>Model.nwc</node>
                  <node>Level 1</node>
                  <node>Walls</node>
                  <node>Basic Wall: ... [123456]</node>
                </pathlink>
              </clashobject>
              <clashobject> ... </clashobject>
            </clashobjects>
          </clashresult>
        </clashresults>
      </clashtest>
    </clashtests>

Two things bite naive parsers:
  * the two clash sides live inside a <clashobjects> container as
    <clashobject> elements (older/other exports use <clashresultpair>
    or <object1>/<object2>);
  * <smarttag> carries its name/value as CHILD elements (<name>/<value>),
    not attributes, and the model path is <pathlink>/<node> text.

This parser handles all of those shapes. Per clash side it collects:
  - name:        the leaf path node (the element)
  - source_file: the path node that looks like a model file
                 (*.nwc/*.rvt/*.ifc/*.dwg/*.nwd), else the top path node
  - guid:        any property whose name contains "guid"
  - numeric_id:  any element-id-like property, else a trailing
                 "[12345]"/"(12345)" pulled from the element name

script.py tries guid first, then numeric_id, so a report is usable as
long as either survived the export. `describe_structure` returns a tag
census used to diagnose a report the parser can't read.
"""

import re
import xml.etree.ElementTree as ET


GUID_TAG_HINTS = ("guid",)
ID_TAG_HINTS = ("elementid", "element id", "revit id", "item id", "id")
NAME_ATTR = "name"

FILE_EXTS = (".nwc", ".rvt", ".ifc", ".dwg", ".nwd", ".dgn", ".skp")
TRAILING_ID_RE = re.compile(r"[\[\(]\s*(\d{3,10})\s*[\]\)]\s*$")

# A filename token (with extension) anywhere inside a path node string.
# Navisworks nodes look like "INTERIORS-AUSTIN R25.rvt : 2 : location <...>"
# or a bare "MODEL.nwc", so match the filename and stop at the extension.
FILE_TOKEN_RE = re.compile(
    r"([^/\\:;<>]+?\.(?:rvt|ifc|dwg|dgn|skp|nwc|nwd))", re.IGNORECASE
)
# Authored-model formats are preferred over Navisworks caches (.nwc/.nwd)
# because they map to Revit link names; a link for INTERIORS.rvt is not
# named after the UTUSB_....nwc cache that contains it.
PREFERRED_EXTS = (".rvt", ".ifc", ".dwg", ".dgn", ".skp")

# Tags that act as a per-side container inside a <clashresult>.
SIDE_CONTAINER_TAGS = ("clashobjects", "clashresultpair", "clashresultpaths")
# Tags that ARE a single clash side.
SIDE_TAGS = ("clashobject", "object1", "object2", "item")


class ClashItem(object):
    def __init__(self, name, source_file, guid, numeric_id):
        self.name = name
        self.source_file = source_file
        self.guid = guid
        self.numeric_id = numeric_id

    def __repr__(self):
        return "ClashItem(name={!r}, source={!r}, guid={!r}, numeric_id={!r})".format(
            self.name, self.source_file, self.guid, self.numeric_id
        )


class ClashPair(object):
    def __init__(self, test_name, result_name, status, item_a, item_b, raw_xml=None):
        self.test_name = test_name
        self.result_name = result_name
        self.status = status
        self.item_a = item_a
        self.item_b = item_b
        self.raw_xml = raw_xml


def _local(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _all_properties(node):
    """Yield (name, value) for every property-like element under node.

    Handles both attribute-style (<smarttag name=".." value="..">) and
    child-element-style (<smarttag><name>..</name><value>..</value>
    </smarttag>) reports, plus <property>/<userdata>/<objectattribute>.
    """
    for el in node.iter():
        if _local(el.tag) not in ("smarttag", "property", "userdata", "objectattribute"):
            continue
        name = el.get(NAME_ATTR) or el.get("Name")
        value = el.get("value") or el.get("Value")
        if name is None or value is None:
            for child in el:
                ctag = _local(child.tag)
                if ctag == "name" and name is None:
                    name = child.text
                elif ctag == "value" and value is None:
                    value = child.text
        name = (name or "").strip()
        value = (value or "").strip()
        if name or value:
            yield name, value


def _find_guid(properties):
    for name, value in properties:
        if any(h in name.lower() for h in GUID_TAG_HINTS) and value:
            return value.strip("{}")
    return None


def _find_numeric_id(properties):
    for name, value in properties:
        lname = name.lower()
        if "guid" in lname:
            continue
        if any(h in lname for h in ID_TAG_HINTS):
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits and digits == value.strip("-"):
                return digits
    return None


def _path_names(node):
    """Ordered path labels for a clash side.

    Covers <item name="..">/<pathitem> and <pathlink>/<node>text</node>."""
    names = []
    for el in node.iter():
        tag = _local(el.tag)
        if tag in ("item", "pathitem", "node"):
            nm = el.get(NAME_ATTR) or el.get("Name") or (el.text or "").strip()
            if nm:
                names.append(nm.strip())
    return names


def _pick_source(names):
    """Choose the model file a clash side came from.

    Scans every path node for filename tokens and prefers an authored
    model (.rvt/.ifc/...) over a Navisworks cache (.nwc/.nwd), since that
    is what matches a Revit link's name. Falls back to any file token,
    then to the first path node."""
    preferred = None
    fallback = None
    for nm in names:
        for m in FILE_TOKEN_RE.finditer(nm):
            token = m.group(1).strip()
            if token.lower().endswith(PREFERRED_EXTS):
                if preferred is None:
                    preferred = token
            elif fallback is None:
                fallback = token
    if preferred:
        return preferred
    if fallback:
        return fallback
    return names[0] if names else None


def _parse_side(node):
    names = _path_names(node)
    source_file = _pick_source(names)
    elem_name = names[-1] if names else node.get(NAME_ATTR, "unknown")

    properties = list(_all_properties(node))
    guid = _find_guid(properties)
    numeric_id = _find_numeric_id(properties)

    if not numeric_id and elem_name:
        m = TRAILING_ID_RE.search(elem_name)
        if m:
            numeric_id = m.group(1)

    return ClashItem(elem_name, source_file, guid, numeric_id)


def _find_two_sides(clashresult_node):
    """Find the two per-side nodes inside a <clashresult>."""
    sides = []
    for child in clashresult_node:
        tag = _local(child.tag)
        if tag in SIDE_TAGS:
            sides.append(child)
        elif tag in SIDE_CONTAINER_TAGS:
            for sub in child:
                if _local(sub.tag) in SIDE_TAGS:
                    sides.append(sub)
    if len(sides) >= 2:
        return sides[0], sides[1]
    return None, None


def _child_text(node, tag_names):
    for el in node.iter():
        if _local(el.tag) in tag_names and el.text:
            return el.text.strip()
    return None


def parse_clash_report(xml_path):
    """Returns a list of ClashPair."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    pairs = []
    for test_node in root.iter():
        if _local(test_node.tag) != "clashtest":
            continue
        test_name = test_node.get(NAME_ATTR, "Unnamed Test")

        for result_node in test_node.iter():
            if _local(result_node.tag) != "clashresult":
                continue
            result_name = result_node.get(NAME_ATTR, "")
            status = (
                result_node.get("status")
                or _child_text(result_node, ("resultstatus", "status"))
                or ""
            )

            side_a, side_b = _find_two_sides(result_node)
            if side_a is None or side_b is None:
                continue

            item_a = _parse_side(side_a)
            item_b = _parse_side(side_b)
            try:
                raw_xml = ET.tostring(result_node, encoding="unicode")
            except Exception:
                try:
                    raw_xml = ET.tostring(result_node)
                except Exception:
                    raw_xml = None
            pairs.append(
                ClashPair(test_name, result_name, status, item_a, item_b, raw_xml)
            )

    return pairs


def distinct_source_files(pairs):
    files = set()
    for p in pairs:
        for item in (p.item_a, p.item_b):
            if item.source_file:
                files.add(item.source_file)
    return sorted(files)


def describe_structure(xml_path, max_sample=1200):
    """Return a human-readable census of the report's tags, so a report
    the parser can't read can be diagnosed without seeing the whole file."""
    try:
        tree = ET.parse(xml_path)
    except Exception as ex:
        return "Could not parse XML at all: {}".format(ex)
    root = tree.getroot()

    counts = {}
    for el in root.iter():
        tag = _local(el.tag)
        counts[tag] = counts.get(tag, 0) + 1

    interesting = [
        "clashtest", "clashresult", "clashobjects", "clashobject",
        "clashresultpair", "object1", "object2", "smarttags", "smarttag",
        "name", "value", "pathlink", "node", "item", "pathitem",
    ]
    lines = ["Root element: <{}>".format(_local(root.tag))]
    lines.append("Tag counts (relevant tags):")
    for tag in interesting:
        if tag in counts:
            lines.append("  <{}>: {}".format(tag, counts[tag]))
    others = sorted(set(counts) - set(interesting))
    if others:
        lines.append("Other tags present: " + ", ".join(others[:40]))

    first = None
    for el in root.iter():
        if _local(el.tag) == "clashresult":
            first = el
            break
    if first is not None:
        try:
            sample = ET.tostring(first, encoding="unicode")
        except Exception:
            sample = ET.tostring(first)
            if isinstance(sample, bytes):
                sample = sample.decode("utf-8", "replace")
        if len(sample) > max_sample:
            sample = sample[:max_sample] + "\n... (truncated)"
        lines.append("\nFirst <clashresult> sample:\n" + sample)
    else:
        lines.append("\nNo <clashresult> elements found anywhere in the file.")

    return "\n".join(lines)
