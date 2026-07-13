bl_info = {
    "name": "Step 2 Blend",
    "author": "Louis Rist (mrrist.com)",
    "version": (7, 3, 0),
    "blender": (3, 0, 0),
    "location": "File › Import › STEP (.step, .stp)  •  View3D Sidebar › Step 2 Blend",
    "description": "Import STEP and STP files into Blender.",
    "doc_url":     "https://github.com/BlueLazyFish/step2blend",
    "tracker_url": "https://github.com/BlueLazyFish/step2blend/issues",
    "support":     "COMMUNITY",
    "category":    "Import-Export",
}

import bpy
import math
import os
import sys
import stat
import time
import shutil
import tempfile
import subprocess
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
from bpy_extras.io_utils import ImportHelper


# ── step2glb discovery ──────────────────────────────────────────────────────

def _addon_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _bundled_step2glb():
    base = os.path.join(_addon_dir(), "bin")
    if sys.platform == "win32":
        p = os.path.join(base, "step2glb.exe")
    else:
        p = os.path.join(base, "step2glb")
    return p if os.path.isfile(p) else None


def _ensure_executable(path):
    if sys.platform == "win32":
        return
    try:
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


def _find_step2glb(custom_path=""):
    if custom_path and os.path.isfile(custom_path):
        _ensure_executable(custom_path)
        return custom_path
    bundled = _bundled_step2glb()
    if bundled:
        _ensure_executable(bundled)
        return bundled
    return None


# ── Modal progress feedback ──────────────────────────────────────────────────
#
# Braille-pattern spinner — one of the few Unicode glyph sets that renders
# consistently in Blender's status bar across platforms. Each frame is a
# different braille pattern; advancing one frame per modal tick (250 ms) gives
# a smooth ~2.5 s rotation that's clearly distinguishable from static text.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ── Units ────────────────────────────────────────────────────────────────────

_UNIT_ITEMS = (
    ("AUTO", "Auto-detect",   "Use the unit declared inside the STEP file (recommended). "
                              "Almost every CAD package writes the unit into the file header, "
                              "so this just works."),
    ("MM",   "Millimeters",  "Override: treat the file as millimeters"),
    ("CM",   "Centimeters",  "Override: treat the file as centimeters"),
    ("M",    "Meters",       "Override: treat the file as meters"),
    ("IN",   "Inches",       "Override: treat the file as inches"),
    ("FT",   "Feet",          "Override: treat the file as feet"),
)

# Post-import object-scale multiplier. AUTO trusts step2glb's length-unit
# conversion (the binary already reads the STEP file's declared unit and
# writes meters into the GLB), so no additional scaling is needed.
# The manual entries are escape-hatch overrides, in case a STEP file lies
# about its declared unit — a rare but real failure mode in legacy CAD.
_UNIT_SCALE = {
    "AUTO": 1.0,
    "MM":   1.0,
    "CM":   10.0,
    "M":    1000.0,
    "IN":   25.4,
    "FT":   304.8,
}


# ── Quality presets ─────────────────────────────────────────────────────────
#
# Chord deflection is absolute, in millimetres of model space — every face
# hits the same physical chord tolerance, producing uniform-looking
# triangles across an assembly regardless of face size. This is what most
# CAD viewers use.
#
# Quality slider (1–5) maps to (linear_mm, angular_rad):
_ABS_QUALITY = {
    1: (4.0,   math.radians(35.0)),  # very coarse
    2: (2.0,   math.radians(28.0)),  # coarse
    3: (1.0,   math.radians(20.0)),  # normal
    4: (0.5,   math.radians(15.0)),  # fine
    5: (0.25,  math.radians(10.0)),  # very fine
}


# ── Friendly color name table ────────────────────────────────────────────────
#
# A curated subset of OCCT's Quantity_NameOfColor enum, written in friendly
# title case so imported materials read as polished names like "S2B Dark Blue"
# rather than developer-y prefixes like "OCCT_DARKBLUE".
#
# Values are RGB floats 0..1 in the space OCCT stores them. Tolerance matching
# falls back to an uppercase hex name (e.g. "FF7E13") when no entry is within
# range — still stable, still readable as a Material DB key.

_NAMED_COLORS = (
    # Grayscale
    ("Black",            0.000, 0.000, 0.000),
    ("White",            1.000, 1.000, 1.000),
    ("Gray",             0.745, 0.745, 0.745),
    ("Gray 10",          0.102, 0.102, 0.102),
    ("Gray 20",          0.200, 0.200, 0.200),
    ("Gray 30",          0.302, 0.302, 0.302),
    ("Gray 40",          0.400, 0.400, 0.400),
    ("Gray 50",          0.498, 0.498, 0.498),
    ("Gray 60",          0.600, 0.600, 0.600),
    ("Gray 70",          0.702, 0.702, 0.702),
    ("Gray 80",          0.800, 0.800, 0.800),
    ("Gray 90",          0.898, 0.898, 0.898),
    ("Dim Gray",         0.412, 0.412, 0.412),
    ("Dark Slate Gray",  0.184, 0.310, 0.310),
    ("Light Gray",       0.827, 0.827, 0.827),
    ("Silver",           0.753, 0.753, 0.753),
    ("Gainsboro",        0.863, 0.863, 0.863),
    ("White Smoke",      0.961, 0.961, 0.961),
    # Red / orange / yellow
    ("Red",              1.000, 0.000, 0.000),
    ("Dark Red",         0.545, 0.000, 0.000),
    ("Firebrick",        0.698, 0.133, 0.133),
    ("Crimson",          0.863, 0.078, 0.235),
    ("Indian Red",       0.804, 0.361, 0.361),
    ("Light Coral",      0.941, 0.502, 0.502),
    ("Salmon",           0.980, 0.502, 0.447),
    ("Tomato",           1.000, 0.388, 0.278),
    ("Coral",            1.000, 0.498, 0.314),
    ("Orange Red",       1.000, 0.271, 0.000),
    ("Orange",           1.000, 0.647, 0.000),
    ("Dark Orange",      1.000, 0.549, 0.000),
    ("Gold",             1.000, 0.843, 0.000),
    ("Yellow",           1.000, 1.000, 0.000),
    ("Khaki",            0.941, 0.902, 0.549),
    ("Dark Khaki",       0.741, 0.718, 0.420),
    # Green
    ("Green",            0.000, 0.502, 0.000),
    ("Dark Green",       0.000, 0.392, 0.000),
    ("Lime",             0.000, 1.000, 0.000),
    ("Lime Green",       0.196, 0.804, 0.196),
    ("Forest Green",     0.133, 0.545, 0.133),
    ("Olive",            0.502, 0.502, 0.000),
    ("Yellow Green",     0.604, 0.804, 0.196),
    ("Chartreuse",       0.498, 1.000, 0.000),
    ("Light Green",      0.565, 0.933, 0.565),
    ("Medium Sea Green", 0.235, 0.702, 0.443),
    ("Sea Green",        0.180, 0.545, 0.341),
    # Cyan / teal
    ("Teal",             0.000, 0.502, 0.502),
    ("Cyan",             0.000, 1.000, 1.000),
    ("Dark Cyan",        0.000, 0.545, 0.545),
    ("Turquoise",        0.251, 0.878, 0.816),
    ("Dark Turquoise",   0.000, 0.808, 0.820),
    ("Light Sea Green",  0.125, 0.698, 0.667),
    # Blue
    ("Blue",             0.000, 0.000, 1.000),
    ("Dark Blue",        0.000, 0.000, 0.545),
    ("Midnight Blue",    0.098, 0.098, 0.439),
    ("Navy",             0.000, 0.000, 0.502),
    ("Royal Blue",       0.255, 0.412, 0.882),
    ("Steel Blue",       0.275, 0.510, 0.706),
    ("Dodger Blue",      0.118, 0.565, 1.000),
    ("Cornflower Blue",  0.392, 0.584, 0.929),
    ("Sky Blue",         0.529, 0.808, 0.922),
    ("Light Blue",       0.678, 0.847, 0.902),
    ("Powder Blue",      0.690, 0.878, 0.902),
    ("Slate Blue",       0.416, 0.353, 0.804),
    ("Slate Gray",       0.439, 0.502, 0.565),
    # Purple / magenta / pink
    ("Purple",           0.502, 0.000, 0.502),
    ("Indigo",           0.294, 0.000, 0.510),
    ("Magenta",          1.000, 0.000, 1.000),
    ("Dark Magenta",     0.545, 0.000, 0.545),
    ("Violet",           0.933, 0.510, 0.933),
    ("Orchid",           0.855, 0.439, 0.839),
    ("Pink",             1.000, 0.753, 0.796),
    ("Hot Pink",         1.000, 0.412, 0.706),
    ("Lavender",         0.902, 0.902, 0.980),
    # Brown / tan / earth
    ("Brown",            0.647, 0.165, 0.165),
    ("Maroon",           0.502, 0.000, 0.000),
    ("Saddle Brown",     0.545, 0.271, 0.075),
    ("Sienna",           0.627, 0.322, 0.176),
    ("Chocolate",        0.824, 0.412, 0.118),
    ("Peru",             0.804, 0.522, 0.247),
    ("Tan",              0.824, 0.706, 0.549),
    ("Burlywood",        0.871, 0.722, 0.529),
    ("Wheat",            0.961, 0.871, 0.702),
    ("Beige",            0.961, 0.961, 0.863),
)

# Prefix added to every renamed material. Brand mark for the addon —
# establishes provenance ("this came in via Step 2 Blend") so users can spot
# imported materials at a glance in the outliner without leaking
# implementation details (no "OCCT", no "glTF").
_MAT_PREFIX = "S2B "


def _nearest_color_name(r, g, b, tol=0.025):
    """Return a friendly title-case color name for an RGB triple, or an
    uppercase hex string ('FF7E13') when no named entry is within `tol`
    Euclidean distance in linear RGB."""
    best_name = None
    best_d2 = (tol * tol) * 3.0
    for name, cr, cg, cb in _NAMED_COLORS:
        d2 = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_name = name
    if best_name is not None:
        return best_name
    return "%02X%02X%02X" % (
        max(0, min(255, int(round(r * 255)))),
        max(0, min(255, int(round(g * 255)))),
        max(0, min(255, int(round(b * 255)))),
    )


# ── Material dedup + rename helpers ──────────────────────────────────────────
#
# OCCT's GLB writer can emit duplicate materials when the same colour appears
# in the STEP file under multiple XCAF style entities, and Blender's glTF
# importer doesn't always collapse them. Both helpers run on the just-imported
# meshes only so we never touch user-authored materials elsewhere in the file.

def _principled_bsdf(mat):
    if not mat or not mat.use_nodes or mat.node_tree is None:
        return None
    return next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


def _material_fingerprint(mat):
    """Stable fingerprint of a 'simple' Principled BSDF — base colour,
    metallic, roughness, alpha (rounded). Returns None for textured /
    custom-graph materials so dedup leaves them untouched."""
    bsdf = _principled_bsdf(mat)
    if bsdf is None:
        return None
    for inp in bsdf.inputs:
        if inp.is_linked:
            return None  # don't fingerprint anything with custom node links
    bc = tuple(round(x, 4) for x in bsdf.inputs["Base Color"].default_value)
    metallic  = round(bsdf.inputs["Metallic"].default_value, 4) if "Metallic" in bsdf.inputs else 0.0
    roughness = round(bsdf.inputs["Roughness"].default_value, 4) if "Roughness" in bsdf.inputs else 0.5
    alpha     = round(bsdf.inputs["Alpha"].default_value, 4) if "Alpha" in bsdf.inputs else 1.0
    return (bc, metallic, roughness, alpha)


def _imported_materials(mesh_objs):
    out = set()
    for obj in mesh_objs:
        if obj.type != "MESH" or obj.data is None:
            continue
        for slot in obj.material_slots:
            if slot.material:
                out.add(slot.material)
    return out


def _dedup_materials(mesh_objs):
    """Merge identical Principled BSDFs across the imported meshes, remap
    slots to a single canonical material, and delete the orphaned dupes."""
    canon = {}     # fingerprint -> canonical material
    aliases = {}   # mat -> canonical mat
    for mat in _imported_materials(mesh_objs):
        fp = _material_fingerprint(mat)
        if fp is None:
            continue
        canon.setdefault(fp, mat)
        aliases[mat] = canon[fp]

    n_remapped = 0
    for obj in mesh_objs:
        if obj.type != "MESH" or obj.data is None:
            continue
        slots = obj.data.materials
        for i in range(len(slots)):
            cur = slots[i]
            new = aliases.get(cur, cur) if cur else cur
            if new is not None and new is not cur:
                slots[i] = new
                n_remapped += 1

    n_deleted = 0
    for mat, canonical in list(aliases.items()):
        if mat is canonical:
            continue
        if mat.users == 0:
            try:
                bpy.data.materials.remove(mat, do_unlink=False)
                n_deleted += 1
            except Exception:
                pass
    return n_deleted, n_remapped


def _rename_materials_friendly(mesh_objs):
    """Rename each imported simple-Principled material to '<prefix><Name>'
    based on its base colour (e.g. 'STEP Dark Blue', 'STEP FF7E13'). Skips
    materials already carrying the prefix and any with linked inputs."""
    n = 0
    for mat in _imported_materials(mesh_objs):
        if mat.name.startswith(_MAT_PREFIX):
            continue
        bsdf = _principled_bsdf(mat)
        if bsdf is None or bsdf.inputs["Base Color"].is_linked:
            continue
        rgba = bsdf.inputs["Base Color"].default_value
        new_name = _MAT_PREFIX + _nearest_color_name(rgba[0], rgba[1], rgba[2])
        if mat.name == new_name:
            continue
        # Avoid Blender's automatic .001 suffix on collision: pick the next
        # free name explicitly so duplicates carry a clear, sortable suffix.
        if new_name in bpy.data.materials and bpy.data.materials[new_name] is not mat:
            i = 1
            while ("%s.%03d" % (new_name, i)) in bpy.data.materials:
                i += 1
            new_name = "%s.%03d" % (new_name, i)
        mat.name = new_name
        n += 1
    return n


# ── Smooth-shading enforcement ───────────────────────────────────────────────
#
# Blender's glTF importer marks individual polygons flat-shaded when the
# analytic vertex normals on a face happen to be (very nearly) parallel to
# the geometric face normal. The intent is a perf optimisation, but for STEP
# imports it's exactly wrong: those polygons stop honouring the per-loop
# custom normals OCCT computed from the underlying NURBS surface, and the
# face renders with a discontinuous flat patch surrounded by smoothly-shaded
# triangles — the classic "slightly strange" surface artefact on what should
# be perfectly smooth tessellated curves. Setting use_smooth=True on every
# polygon makes Blender always interpolate via the custom split normals, so
# the analytic shading propagates uniformly across the part.


def _force_smooth_shading(mesh_objs):
    """Force every polygon of every imported mesh to use smooth shading so
    the OCCT analytic split normals are always honoured. Returns the number
    of meshes touched."""
    n = 0
    for obj in mesh_objs:
        if obj.type != "MESH" or obj.data is None:
            continue
        m = obj.data
        n_polys = len(m.polygons)
        if n_polys == 0:
            continue
        m.polygons.foreach_set("use_smooth", [True] * n_polys)
        m.update()
        n += 1
    return n


# ── Mesh-instance deduplication ──────────────────────────────────────────────
#
# Big assemblies often contain dozens of identical hardware parts — bolts,
# washers, bearings — each one imported as a separate mesh datablock by the
# glTF importer. We hash the geometry (positions + indices + material slot
# names) and link `obj.data` for matches, so 80 identical M4x16 bolts share
# one mesh instead of carrying 80 copies. Editing the master mesh propagates
# to every linked object — exactly the SolidWorks-style instancing behaviour
# CAD users expect, with a real memory and viewport-perf win for large models.
#
# Material slot names are part of the hash because Blender treats materials
# as part of mesh data: linking obj.data also links materials. Two parts with
# identical geometry but different colours stay distinct.

import struct as _struct
import hashlib as _hashlib


def _mesh_geometry_hash(mesh):
    """Stable 16-byte hash of vertex positions + face indices + material
    slot names. Positions are quantised to 5 decimal places so floating-point
    jitter from independent tessellation runs doesn't break matches."""
    h = _hashlib.blake2b(digest_size=16)

    n_verts = len(mesh.vertices)
    h.update(_struct.pack("I", n_verts))
    if n_verts > 0:
        coords = [0.0] * (n_verts * 3)
        mesh.vertices.foreach_get("co", coords)
        # 1e-5 mm precision is far below any STEP author tolerance and well
        # below the BRepMesh chord deflection we're emitting at.
        h.update(_struct.pack(
            "%di" % len(coords),
            *(int(round(c * 100000.0)) for c in coords),
        ))

    n_loops = len(mesh.loops)
    h.update(_struct.pack("I", n_loops))
    if n_loops > 0:
        idx = [0] * n_loops
        mesh.loops.foreach_get("vertex_index", idx)
        h.update(_struct.pack("%dI" % n_loops, *idx))

    # Polygon material indices + the mesh's material slot names. This
    # captures both "same geometry but slot 0 is red vs blue" and "slot
    # ordering swapped" cases.
    n_polys = len(mesh.polygons)
    if n_polys > 0:
        mat_idx = [0] * n_polys
        mesh.polygons.foreach_get("material_index", mat_idx)
        h.update(_struct.pack("%dI" % n_polys, *mat_idx))
    h.update("|".join(m.name if m else "" for m in mesh.materials).encode("utf-8"))

    return h.digest()


def _dedup_imported_meshes(mesh_objs):
    """Find groups of objects with byte-identical geometry+materials and
    re-link them all to a single canonical mesh datablock. Returns
    (instances_relinked, mesh_datablocks_removed, unique_geometries)."""
    canonical = {}    # hash -> first mesh seen with that hash
    obj_hash  = {}    # obj   -> hash

    for obj in mesh_objs:
        if obj.type != "MESH" or obj.data is None:
            continue
        h = _mesh_geometry_hash(obj.data)
        obj_hash[obj] = h
        canonical.setdefault(h, obj.data)

    n_relinked = 0
    orphans = set()
    for obj, h in obj_hash.items():
        canon = canonical[h]
        if obj.data is not canon:
            orphans.add(obj.data)
            obj.data = canon
            n_relinked += 1

    n_removed = 0
    for m in orphans:
        # Only remove if no remaining references (other objects, animations,
        # node trees, etc). Better to leak a datablock than break references.
        if m.users == 0:
            try:
                bpy.data.meshes.remove(m)
                n_removed += 1
            except Exception:
                pass

    return n_relinked, n_removed, len(canonical)


# ── Smart UV Project helper ──────────────────────────────────────────────────

def _smart_uv_project(context, obj):
    for o in context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj

    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        try:
            bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.02)
        except TypeError:
            bpy.ops.uv.smart_project(angle_limit=66.0, island_margin=0.02)
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as e:
        print("[Step 2 Blend] Smart UV Project failed on %s: %s" % (obj.name, e))
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass


# ── Addon preferences ────────────────────────────────────────────────────────

class STEP_IMPORTER_Prefs(bpy.types.AddonPreferences):
    bl_idname = __name__

    step2glb_path: StringProperty(
        name="Custom step2glb Path",
        description="Override path to step2glb executable. Leave empty to use the bundled binary.",
        default="",
        subtype="FILE_PATH",
    )
    def draw(self, context):
        layout = self.layout

        # Blender's addon entry already renders the name, version,
        # description, maintainer, Website link, and Report-a-Bug link from
        # bl_info — the only thing left worth surfacing in this pane is the
        # live runtime status (which binary is in use). Anything else here
        # would just be duplicate noise.

        # ── Engine status ────────────────────────────────────────────────
        engine = layout.box()
        engine.label(text="Conversion engine", icon="SETTINGS")
        found = _find_step2glb(self.step2glb_path)
        bundled = _bundled_step2glb()
        if bundled:
            engine.label(text="Bundled: %s" % bundled, icon="PACKAGE")
        if found:
            engine.label(text="In use: %s" % found, icon="CHECKMARK")
        else:
            engine.label(text="step2glb binary not found.", icon="ERROR")
        engine.prop(self, "step2glb_path")


# ── Operator ─────────────────────────────────────────────────────────────────

class IMPORT_OT_step(bpy.types.Operator, ImportHelper):
    bl_idname = "import_mesh.step"
    bl_label = "Import"
    bl_description = "Open and convert a STEP or STP file"
    bl_options = {"REGISTER", "UNDO"}

    filter_glob: StringProperty(default="*.step;*.stp;*.STEP;*.STP", options={"HIDDEN"})

    unit: EnumProperty(
        name="File Unit",
        description=(
            "Unit of measurement used inside the STEP file. Auto-detect reads "
            "the unit from the file header (almost always correct). Manual "
            "options are an override for the rare case of a mis-tagged file."
        ),
        items=_UNIT_ITEMS,
        default="AUTO",
    )
    quality: IntProperty(
        name="Quality",
        description=(
            "1 = very coarse (4 mm chord), 3 = balanced (1 mm), "
            "5 = very fine (0.25 mm). Higher values produce more triangles."
        ),
        default=3, min=1, max=5, subtype="FACTOR",
    )
    tidy_materials: BoolProperty(
        name="Tidy Materials",
        description=(
            "Deduplicate identical materials and rename them based on base "
            "colour (e.g. 'S2B Dark Blue', 'S2B Red', 'S2B FF7E13')"
        ),
        default=True,
    )
    smart_uv: BoolProperty(
        name="Smart UV Project",
        description="Automatically generate UVs using Smart UV Project after import",
        default=True,
    )
    create_collection: BoolProperty(
        name="Group Bodies in Collection",
        description="Put all imported bodies inside a new collection named after the file",
        default=True,
    )
    center_on_origin: BoolProperty(
        name="Center on Origin",
        description=(
            "Translate the import so its bounding-box centre sits at world "
            "(0, 0, 0). Useful for CAD models exported far from origin"
        ),
        default=False,
    )
    auto_frame: BoolProperty(
        name="Auto-frame in Viewport",
        description="After importing, frame the model in the 3D viewport so it's centred and visible",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        col = layout.column(align=True)
        col.prop(self, "unit")

        layout.separator()
        box = layout.box()
        box.label(text="Quality")
        row = box.row(align=True)
        row.label(text="Coarse", icon="TRIA_LEFT")
        row.prop(self, "quality", text="")
        row.label(text="Fine", icon="TRIA_RIGHT")
        chord_mm, ang_rad = _ABS_QUALITY[self.quality]
        box.label(
            text="chord %.2f mm  /  angle %d°" % (chord_mm, round(math.degrees(ang_rad))),
            icon="INFO",
        )

        layout.separator()
        col = layout.column(heading="Options")
        col.prop(self, "tidy_materials")
        col.prop(self, "smart_uv")
        col.prop(self, "create_collection")
        col.prop(self, "center_on_origin")
        col.prop(self, "auto_frame")

    # Modal state — populated in execute(), consumed in modal()/_finish().
    _proc       = None
    _timer      = None
    _tempdir    = None
    _glb_path   = None
    _basename   = ""
    _t_start    = 0.0
    _timeout_s  = 600
    _spinner_i  = 0
    _cursor_set = False

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        exe = _find_step2glb(prefs.step2glb_path)
        if not exe:
            self.report(
                {"ERROR"},
                "step2glb binary not found. Reinstall the addon or set a custom path in Preferences.",
            )
            return {"CANCELLED"}

        self._basename = os.path.splitext(os.path.basename(self.filepath))[0]
        self._tempdir  = tempfile.mkdtemp(prefix="blender_step_")
        self._glb_path = os.path.join(self._tempdir, "out.glb")

        linear_mm, angular_rad = _ABS_QUALITY[self.quality]
        cmd = [
            exe, self.filepath, self._glb_path,
            "--linear", str(linear_mm),
            "--angular", str(angular_rad),
        ]
        print("[Step 2 Blend] Running: %s" % " ".join(cmd))

        # Start step2glb non-blocking and let a modal timer poll completion.
        # Blender's UI stays responsive; status bar shows elapsed time and
        # the user can press Esc to cancel.
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self.report({"ERROR"}, "Failed to launch step2glb: %s" % exc)
            return {"CANCELLED"}

        self._t_start = time.time()
        self._spinner_i = 0
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.25, window=context.window)
        wm.modal_handler_add(self)

        # Wait cursor + spinner status text gives the user two simultaneous
        # cues that work is in progress, so the import never feels frozen.
        try:
            context.window.cursor_modal_set("WAIT")
            self._cursor_set = True
        except Exception:
            self._cursor_set = False

        context.workspace.status_text_set(
            "%s  Step 2 Blend  •  %s  •  starting… (Esc to cancel)"
            % (_SPINNER[0], self._basename)
        )
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        # Esc cancels at any point.
        if event.type == "ESC":
            return self._cancel(context, "Cancelled by user.")

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        # Advance spinner + repaint status bar. The Braille glyph rotates
        # every 250 ms — a clear, animated indicator that the importer is
        # alive even on long runs where elapsed time alone could read like a
        # frozen clock.
        self._spinner_i = (self._spinner_i + 1) % len(_SPINNER)
        glyph = _SPINNER[self._spinner_i]
        elapsed = time.time() - self._t_start
        context.workspace.status_text_set(
            "%s  Step 2 Blend  •  %s  •  %.1fs  •  Esc to cancel"
            % (glyph, self._basename, elapsed)
        )

        # Hard timeout safety net.
        if elapsed > self._timeout_s:
            return self._cancel(
                context,
                "step2glb timed out after %d minutes." % (self._timeout_s // 60),
            )

        ret = self._proc.poll()
        if ret is None:
            return {"RUNNING_MODAL"}
        return self._finish(context, ret)

    def _release_modal(self, context):
        """Detach timer, clear status bar, restore cursor, drain process pipes."""
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        if self._cursor_set:
            try:
                context.window.cursor_modal_restore()
            except Exception:
                pass
            self._cursor_set = False
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

    def _cancel(self, context, message):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._release_modal(context)
        if self._tempdir:
            shutil.rmtree(self._tempdir, ignore_errors=True)
        self.report({"WARNING"}, message)
        return {"CANCELLED"}

    def _finish(self, context, ret):
        self._release_modal(context)

        # Drain stdout/stderr now that the process has exited.
        try:
            stdout, stderr = self._proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        if stdout:
            print("[Step 2 Blend] step2glb stdout:\n" + stdout)
        if stderr:
            print("[Step 2 Blend] step2glb stderr:\n" + stderr)

        if not os.path.isfile(self._glb_path):
            shutil.rmtree(self._tempdir, ignore_errors=True)
            tail = (stderr or stdout or "").strip().splitlines()[-1:]
            self.report(
                {"ERROR"},
                "step2glb produced no output (exit %d). %s" % (ret, (tail[0] if tail else "")[:200]),
            )
            return {"CANCELLED"}

        # Snapshot existing top-level objects so we can isolate what gltf imports.
        before = set(o.name for o in bpy.data.objects)

        try:
            bpy.ops.import_scene.gltf(filepath=self._glb_path)
        except Exception as e:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self.report({"ERROR"}, "glTF import failed: %s" % e)
            return {"CANCELLED"}

        shutil.rmtree(self._tempdir, ignore_errors=True)

        imported = [o for o in bpy.data.objects if o.name not in before]
        mesh_objs = [o for o in imported if o.type == "MESH"]

        if not imported:
            self.report({"WARNING"}, "step2glb GLB imported no objects.")
            return {"CANCELLED"}

        # Apply unit scale by scaling top-level (parentless) imported objects.
        s = _UNIT_SCALE[self.unit]
        if s != 1.0:
            for o in imported:
                if o.parent is None:
                    o.scale = (o.scale[0] * s, o.scale[1] * s, o.scale[2] * s)

        # Move into a named collection if requested.
        if self.create_collection:
            col_blender = bpy.data.collections.new(self._basename)
            context.scene.collection.children.link(col_blender)
            for o in imported:
                for c in list(o.users_collection):
                    c.objects.unlink(o)
                col_blender.objects.link(o)

        # Tidy materials BEFORE stamping STEP_materials so the recorded
        # original-name keys are the friendly 'S2B <Name>' strings instead
        # of whatever raw names came out of the glTF importer (Material_3,
        # etc). Keys built against tidy names stay stable across re-imports
        # of the same file.
        if self.tidy_materials:
            n_del, n_remap = _dedup_materials(mesh_objs)
            n_named = _rename_materials_friendly(mesh_objs)
            if (n_del or n_remap or n_named):
                print(
                    "[Step 2 Blend] Tidy materials: %d duplicates merged, "
                    "%d slots remapped, %d renamed."
                    % (n_del, n_remap, n_named)
                )

        # Force smooth shading on every polygon so OCCT's analytic per-loop
        # normals are honoured everywhere. The glTF importer otherwise marks
        # ~2-3% of polygons flat-shaded and those patches lose their custom
        # normals — the source of the "slightly faceted on smooth surfaces"
        # artefact users notice on shaded curves. Done before dedup so the
        # canonical mesh datablocks all carry the fix when linked.
        _force_smooth_shading(mesh_objs)

        # Mesh-instance dedup. Runs AFTER material rename so the geometry
        # hash uses friendly material names — assemblies with 80 identical
        # bolts collapse to one shared mesh datablock.
        n_relinked, n_meshes_removed, n_unique = _dedup_imported_meshes(mesh_objs)
        if n_relinked:
            print(
                "[Step 2 Blend] Mesh dedup: %d objects re-linked to %d unique "
                "geometries (%d duplicate datablocks removed)."
                % (n_relinked, n_unique, n_meshes_removed)
            )

        # Smart UV on every mesh.
        if self.smart_uv:
            for obj in mesh_objs:
                _smart_uv_project(context, obj)

        # Optional: centre the import on world origin. Computed from the
        # union bounding box of every imported mesh in world space, then
        # subtracted from each top-level (parentless) object's location so
        # children inherit the translation cleanly.
        if self.center_on_origin and mesh_objs:
            import mathutils
            coords = []
            for o in mesh_objs:
                for corner in o.bound_box:
                    coords.append(o.matrix_world @ mathutils.Vector(corner))
            if coords:
                center = sum(coords, mathutils.Vector((0.0, 0.0, 0.0))) / len(coords)
                for o in imported:
                    if o.parent is None:
                        o.location -= center

        # Final selection: all imported meshes. Sets the active object too
        # so the next viewport operation has a target.
        for o in context.selected_objects:
            o.select_set(False)
        for o in mesh_objs:
            o.select_set(True)
        if mesh_objs:
            context.view_layer.objects.active = mesh_objs[0]

        # Auto-frame: the user just hit Import — the very next thing they
        # should see is their model, not the world origin. Walk every 3D
        # viewport in the active screen and frame the selection in each.
        if self.auto_frame and mesh_objs:
            for area in context.window.screen.areas:
                if area.type != "VIEW_3D":
                    continue
                for region in area.regions:
                    if region.type == "WINDOW":
                        try:
                            with context.temp_override(area=area, region=region):
                                bpy.ops.view3d.view_selected(use_all_regions=False)
                        except Exception:
                            pass
                        break

        # Summary toast — lands in Blender's Info panel and as a status-bar
        # flash. Tells the buyer at a glance "yes, the work happened."
        elapsed = time.time() - self._t_start
        n_parts = len(mesh_objs)
        n_colors = len({m.name for o in mesh_objs for m in o.data.materials if m})
        n_unique = len({o.data.name for o in mesh_objs if o.data})
        self.report(
            {"INFO"},
            "Step 2 Blend  •  %d part%s  •  %d colour%s  •  %d unique geometr%s  •  %.1fs"
            % (
                n_parts,  "s"   if n_parts  != 1 else "",
                n_colors, "s"   if n_colors != 1 else "",
                n_unique, "ies" if n_unique != 1 else "y",
                elapsed,
            ),
        )
        return {"FINISHED"}


# ── Sidebar panel ────────────────────────────────────────────────────────────

class VIEW3D_PT_s2b(bpy.types.Panel):
    bl_label = "Step 2 Blend"
    bl_idname = "VIEW3D_PT_step"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Step 2 Blend"

    def draw(self, context):
        layout = self.layout
        prefs = context.preferences.addons[__name__].preferences
        exe = _find_step2glb(prefs.step2glb_path)
        if not exe:
            col = layout.column(align=True)
            col.label(text="step2glb not found", icon="ERROR")
            col.label(text="See addon preferences")
            return

        # Simple, clean import button — the panel header already says
        # "Step 2 Blend", so the button just needs to say what it does.
        layout.operator("import_mesh.step", text="Import .STEP", icon="IMPORT")


def _menu_func(self, context):
    self.layout.operator(IMPORT_OT_step.bl_idname, text="STEP (.step, .stp)")


_classes = (
    STEP_IMPORTER_Prefs,
    IMPORT_OT_step,
    VIEW3D_PT_s2b,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_func)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_func)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
