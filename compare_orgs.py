#!/usr/bin/env python3
"""
compare_orgs.py — Compare Salesforce metadata between two orgs.

Pipeline:
  1. Retrieve a fixed set of metadata types from each org (new `sf` CLI).
  2. Canonicalize every XML file (pretty-print + sort sibling elements) so that
     non-deterministic element ordering from the Metadata API stops producing
     false differences.
  3. Walk both trees and report: only-in-A, only-in-B, and changed files
     (with unified diffs that optionally ignore blank lines / trailing space).

Requires: Python 3.9+ and the Salesforce CLI (`sf`) authenticated to both orgs.

Usage:
  python compare_orgs.py <orgA-alias-or-username> <orgB-alias-or-username>
  python compare_orgs.py prod uat --out ./drift
  python compare_orgs.py prod uat --skip-retrieve      # re-diff without re-downloading
  python compare_orgs.py prod uat --no-sort            # order-sensitive comparison
  python compare_orgs.py prod uat --no-ignore-whitespace

Exit codes: 0 = no differences, 1 = differences found, 2 = error.
"""

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# --- Metadata types to compare ------------------------------------------------
# "Apex" = ApexClass + ApexTrigger. Add ApexPage / ApexComponent here if you
# also want Visualforce. CustomObject (source format) decomposes into fields,
# validation rules, record types, list views, etc. — i.e. "entire object related".
DEFAULT_METADATA = [
    "ApexClass",
    "ApexTrigger",
    "PermissionSet",
    "PermissionSetGroup",
    "Flow",
    "Layout",
    "Profile",
    "CustomObject",
    "CustomLabels",
    "CustomMetadata",
    "LightningComponentBundle",
    "FlexiPage",
    "StandardValueSet",
]

SF_METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
SFDX_PROJECT = {
    "packageDirectories": [{"path": "force-app", "default": True}],
    "name": "org-compare",
    "namespace": "",
    "sfdcLoginUrl": "https://login.salesforce.com",
    "sourceApiVersion": "67.0",
}

# Make ElementTree emit the default namespace without ns0: prefixes.
ET.register_namespace("", SF_METADATA_NS)


# --- sf CLI helpers -----------------------------------------------------------
def find_sf():
    for name in ("sf", "sf.cmd"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("ERROR: Salesforce CLI (`sf`) not found on PATH. Install it and authenticate both orgs.")


def run(cmd, cwd=None):
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def scaffold_project(org_dir: Path):
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / "force-app").mkdir(exist_ok=True)
    (org_dir / "sfdx-project.json").write_text(json.dumps(SFDX_PROJECT, indent=2))


def retrieve_org(sf, org_dir: Path, org: str, metadata: list[str]):
    print(f"\n==> Retrieving '{org}' into {org_dir}")
    scaffold_project(org_dir)
    cmd = [sf, "project", "retrieve", "start", "--target-org", org, "--wait", "33"]
    for m in metadata:
        cmd += ["--metadata", m]
    result = run(cmd, cwd=str(org_dir))
    if result.stdout:
        print(result.stdout.strip()[-2000:])
    if result.returncode != 0:
        # Missing manifest members are warnings, not fatal; a real failure (auth,
        # network) leaves the tree empty, which we check below.
        print(f"  ! sf returned {result.returncode}:\n{result.stderr.strip()[-2000:]}")
    force_app = org_dir / "force-app"
    if not any(force_app.rglob("*")):
        sys.exit(
            f"ERROR: nothing was retrieved for '{org}'. Check `sf org display -o {org}` "
            "and that the org is authenticated."
        )


# --- XML canonicalization -----------------------------------------------------
def _canonicalize_elem(elem: ET.Element):
    # Sort children depth-first (post-order) so a parent's sort key is stable.
    for child in list(elem):
        _canonicalize_elem(child)
    # Normalize attribute order.
    if elem.attrib:
        elem.attrib = dict(sorted(elem.attrib.items()))
    # Drop insignificant whitespace so the pretty-printer can re-indent.
    if elem.text is not None:
        elem.text = elem.text.strip() or None
    if elem.tail is not None:
        elem.tail = elem.tail.strip() or None
    # Sort siblings by their canonical serialized form (children already sorted).
    children = list(elem)
    if children:
        children.sort(key=lambda e: ET.tostring(e, encoding="unicode"))
        elem[:] = children


def canonicalize_file(path: Path):
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        print(f"  ! skipping (not parseable XML): {path} ({exc})")
        return
    root = tree.getroot()
    _canonicalize_elem(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def normalize_tree(force_app: Path, sort: bool):
    if not sort:
        return
    print(f"\n==> Canonicalizing XML under {force_app}")
    count = 0
    for path in force_app.rglob("*"):
        if path.is_file() and path.name.endswith(".xml"):
            canonicalize_file(path)
            count += 1
    print(f"    normalized {count} XML files")


# --- Comparison ---------------------------------------------------------------
def read_text(path: Path):
    return path.read_text(encoding="utf-8", errors="replace")


def lines_for_compare(text: str, ignore_ws: bool):
    lines = text.splitlines()
    if ignore_ws:
        lines = [ln.rstrip() for ln in lines]
        lines = [ln for ln in lines if ln.strip() != ""]
    return lines


def build_hunks(la, lb, context=3):
    """Return PR-style hunks of aligned rows for the HTML view.

    Each row: {t, ln, lt, rn, rt}
      t  = type: 'eq' | 'del' | 'ins' | 'rep'
      ln = left line number  (or None), lt = left text  (or None)
      rn = right line number (or None), rt = right text (or None)
    """
    sm = difflib.SequenceMatcher(a=la, b=lb)
    hunks = []
    for group in sm.get_grouped_opcodes(context):
        rows = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    rows.append({"t": "eq", "ln": i1 + k + 1, "lt": la[i1 + k],
                                 "rn": j1 + k + 1, "rt": lb[j1 + k]})
            elif tag == "delete":
                for k in range(i1, i2):
                    rows.append({"t": "del", "ln": k + 1, "lt": la[k], "rn": None, "rt": None})
            elif tag == "insert":
                for k in range(j1, j2):
                    rows.append({"t": "ins", "ln": None, "lt": None, "rn": k + 1, "rt": lb[k]})
            else:  # replace
                left, right = la[i1:i2], lb[j1:j2]
                for k in range(max(len(left), len(right))):
                    lt = left[k] if k < len(left) else None
                    rt = right[k] if k < len(right) else None
                    rows.append({"t": "rep",
                                 "ln": (i1 + k + 1) if lt is not None else None, "lt": lt,
                                 "rn": (j1 + k + 1) if rt is not None else None, "rt": rt})
        g0, g1 = group[0], group[-1]
        header = f"@@ -{g0[1] + 1},{g1[2] - g0[1]} +{g0[3] + 1},{g1[4] - g0[3]} @@"
        hunks.append({"header": header, "rows": rows})
    return hunks


TYPE_FOLDERS = {
    "classes": "ApexClass", "triggers": "ApexTrigger", "permissionsets": "PermissionSet",
    "permissionsetgroups": "PermissionSetGroup", "flows": "Flow", "layouts": "Layout",
    "profiles": "Profile", "objects": "CustomObject", "labels": "CustomLabels",
    "customMetadata": "CustomMetadata", "lwc": "LightningComponentBundle",
    "flexipages": "FlexiPage", "standardValueSets": "StandardValueSet", "pages": "ApexPage",
    "components": "ApexComponent",
}


def component_type(rel: str) -> str:
    """Derive a friendly metadata type from a source-format relative path."""
    parts = rel.split("/")
    for i, p in enumerate(parts):
        if p in TYPE_FOLDERS:
            t = TYPE_FOLDERS[p]
            if p == "objects" and i + 1 < len(parts):
                return f"{t} ({parts[i + 1]})"  # include object API name
            return t
    return parts[-2] if len(parts) > 1 else "Unknown"


def rel_map(force_app: Path) -> dict[str, Path]:
    out = {}
    for path in force_app.rglob("*"):
        if path.is_file():
            out[str(path.relative_to(force_app)).replace(os.sep, "/")] = path
    return out


def compare_trees(fa_a: Path, fa_b: Path, out: Path, ignore_ws: bool):
    a, b = rel_map(fa_a), rel_map(fa_b)
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    common = sorted(set(a) & set(b))

    changed = []
    changed_data = []  # structured hunks for the HTML report
    diffs_dir = out / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)

    for rel in common:
        la = lines_for_compare(read_text(a[rel]), ignore_ws)
        lb = lines_for_compare(read_text(b[rel]), ignore_ws)
        if la != lb:
            changed.append(rel)
            diff = difflib.unified_diff(la, lb, fromfile=f"A/{rel}", tofile=f"B/{rel}", lineterm="")
            flat = rel.replace("/", "__") + ".diff"
            (diffs_dir / flat).write_text("\n".join(diff) + "\n", encoding="utf-8")
            hunks = build_hunks(la, lb)
            adds = sum(1 for h in hunks for r in h["rows"] if r["rt"] is not None and r["t"] != "eq")
            dels = sum(1 for h in hunks for r in h["rows"] if r["lt"] is not None and r["t"] != "eq")
            changed_data.append({"path": rel, "hunks": hunks, "adds": adds, "dels": dels})

    return only_a, only_b, changed, len(common), changed_data


def write_report(out: Path, org_a, org_b, only_a, only_b, changed, common_count):
    lines = [
        "# Org metadata comparison",
        "",
        f"- Org A: `{org_a}`",
        f"- Org B: `{org_b}`",
        "",
        "## Summary",
        "",
        f"- Only in A: **{len(only_a)}**",
        f"- Only in B: **{len(only_b)}**",
        f"- Changed (in both, differ): **{len(changed)}**",
        f"- Identical: **{common_count - len(changed)}** of {common_count} common files",
        "",
    ]

    def section(title, items, note=""):
        block = [f"## {title} ({len(items)})", ""]
        if note:
            block += [note, ""]
        block += [f"- `{i}`" for i in items] or ["_none_"]
        block.append("")
        return block

    lines += section(f"Only in A ({org_a})", only_a)
    lines += section(f"Only in B ({org_b})", only_b)
    lines += section(
        "Changed", changed, "Per-file unified diffs are in the `diffs/` folder."
    )
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


# Template is loaded at runtime from report_template.html (same directory as this script).
# Edit that file directly to customise the look of the HTML report.


def render_report_html(out: Path, data: dict):
    template_path = Path(__file__).parent / "report_template.html"
    if not template_path.exists():
        sys.exit(f"ERROR: report_template.html not found next to the script ({template_path}). "
                 "Keep both files in the same directory.")
    template = template_path.read_text(encoding="utf-8")
    html = (template
            .replace("__ORG_A__", str(data["org_a"]))
            .replace("__ORG_B__", str(data["org_b"]))
            .replace("__DIFF_DATA__", json.dumps(data, ensure_ascii=False)))
    (out / "report.html").write_text(html, encoding="utf-8")


def _diff_rows(data: dict):
    """Yield (file, change, type, added, removed) rows from the diff data."""
    org_a, org_b = data["org_a"], data["org_b"]
    for f in data["changed"]:
        yield (f["path"], "Changed", component_type(f["path"]), f["adds"], f["dels"])
    for p in data["only_a"]:
        yield (p, f"Only in A ({org_a})", component_type(p), "", "")
    for p in data["only_b"]:
        yield (p, f"Only in B ({org_b})", component_type(p), "", "")


def build_csv(out: Path, data: dict):
    import csv
    headers = ["File", "Change", "Component type", "Lines added", "Lines removed"]
    csv_path = out / "report.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(_diff_rows(data))
    return csv_path


def write_html_report(out: Path, org_a, org_b, only_a, only_b, changed_data):
    data = {
        "org_a": org_a, "org_b": org_b,
        "only_a": only_a, "only_b": only_b,
        "changed": changed_data,
    }
    # Cache the computed diff so the HTML can be re-rendered later with --html-only.
    (out / "diff_data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    render_report_html(out, data)


# --- Main ---------------------------------------------------------------------
def main():
    if sys.version_info < (3, 9):
        sys.exit("ERROR: Python 3.9+ required (uses xml.etree.ElementTree.indent).")

    ap = argparse.ArgumentParser(description="Compare Salesforce metadata between two orgs.")
    ap.add_argument("org_a", nargs="?", help="Alias or username for org A")
    ap.add_argument("org_b", nargs="?", help="Alias or username for org B")
    ap.add_argument("--out", default="./org-compare", help="Output directory (default ./org-compare)")
    ap.add_argument("--metadata", nargs="+", default=DEFAULT_METADATA, help="Override metadata types")
    ap.add_argument("--skip-retrieve", action="store_true", help="Reuse already-retrieved trees")
    ap.add_argument("--html-only", action="store_true",
                    help="Re-render report.html from cached diff_data.json (no retrieve/compare)")
    ap.add_argument("--no-sort", action="store_true", help="Disable element sorting (order-sensitive diff)")
    ap.add_argument("--no-ignore-whitespace", action="store_true",
                    help="Treat blank-line / trailing-space differences as real")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    dir_a, dir_b = out / "orgA", out / "orgB"
    sort = not args.no_sort
    ignore_ws = not args.no_ignore_whitespace

    # Fast path: just re-render the HTML from the previously cached diff data.
    if args.html_only:
        data_path = out / "diff_data.json"
        if not data_path.exists():
            sys.exit(f"ERROR: {data_path} not found. Run a full comparison first.")
        data = json.loads(data_path.read_text(encoding="utf-8"))
        render_report_html(out, data)
        build_csv(out, data)
        print(f"Re-rendered {out / 'report.html'} from cached data.")
        sys.exit(0)

    if not args.org_a or not args.org_b:
        sys.exit("ERROR: provide both org aliases, e.g. `compare_orgs.py prod uat` "
                 "(or use --html-only to re-render from cached data).")

    if not args.skip_retrieve:
        sf = find_sf()
        for d in (dir_a, dir_b):
            if (d / "force-app").exists():
                shutil.rmtree(d / "force-app")
        retrieve_org(sf, dir_a, args.org_a, args.metadata)
        retrieve_org(sf, dir_b, args.org_b, args.metadata)

    fa_a, fa_b = dir_a / "force-app", dir_b / "force-app"
    if not fa_a.exists() or not fa_b.exists():
        sys.exit("ERROR: retrieved trees not found. Run without --skip-retrieve first.")

    normalize_tree(fa_a, sort)
    normalize_tree(fa_b, sort)

    only_a, only_b, changed, common_count, changed_data = compare_trees(fa_a, fa_b, out, ignore_ws)
    write_report(out, args.org_a, args.org_b, only_a, only_b, changed, common_count)
    write_html_report(out, args.org_a, args.org_b, only_a, only_b, changed_data)
    data = json.loads((out / "diff_data.json").read_text(encoding="utf-8"))
    build_csv(out, data)

    print("\n" + "=" * 60)
    print(f"Only in A ({args.org_a}): {len(only_a)}")
    print(f"Only in B ({args.org_b}): {len(only_b)}")
    print(f"Changed:                  {len(changed)}")
    print(f"Identical:                {common_count - len(changed)} / {common_count} common")
    print(f"\nVisual:     {out / 'report.html'}   <- open this in a browser")
    print(f"Report:     {out / 'report.md'}")
    print(f"CSV:        {out / 'report.csv'}")
    print(f"Diffs:      {out / 'diffs'}")
    print("=" * 60)

    sys.exit(0 if not (only_a or only_b or changed) else 1)


if __name__ == "__main__":
    main()