# Material Database for the STEP File Importer.
#
# Lets users define {original_step_material → replacement_blender_material}
# mappings, persist them as .blend files in the user-config directory, and
# auto-replace materials on every future STEP import.
#
# Architecture (mirrors STEPper NEXT, adapted for our GLB pipeline):
#
#   Per-imported-object:
#     obj["STEP_materials"] = JSON list of original slot material names.
#     Stamped right after bpy.ops.import_scene.gltf() finishes.
#
#   Per-database (one .blend file in the user-config matdb dir):
#     - bpy.data.texts["STEPper_MaterialDB"]: JSON {orig_name: repl_name}
#     - One copy of each replacement Material (with fake_user)
#     - Saved via bpy.data.libraries.write
#
#   Apply path:
#     For each imported mesh, iterate its slots. For each slot index i,
#     look up obj["STEP_materials"][i] in the mappings and swap if hit.
#     Always uses the *recorded* original, so re-applying is idempotent.

import os
import json
import shutil
from collections import Counter

import bpy
from bpy.props import (
    StringProperty,
    BoolProperty,
    IntProperty,
    EnumProperty,
    CollectionProperty,
    PointerProperty,
)


_DB_TEXT_NAME = "STEPper_MaterialDB"
_OBJ_PROP = "STEP_materials"


# ── Paths ────────────────────────────────────────────────────────────────────

def matdb_dir():
    """User-config dir for material databases. Survives addon reinstalls."""
    base = bpy.utils.user_resource("CONFIG", path="step_importer_matdb", create=True)
    return base


def _db_path(name):
    if not name.endswith(".blend"):
        name += ".blend"
    return os.path.join(matdb_dir(), name)


def list_databases():
    """Sorted list of database names (without .blend extension)."""
    d = matdb_dir()
    if not os.path.isdir(d):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(d)
        if f.endswith(".blend") and not f.startswith(".")
    )


# Memoization cache for the EnumProperty items() callback. Blender stores
# raw C pointers to the strings returned from items(); if Python ever
# garbage-collects those tuples, the pointers dangle and you get spurious
# "current value 'N' matches no enum" warnings plus TypeError on assignment.
#
# Strategy: dict keyed on the tuple of database names. Identical inputs
# return the same physical list, and every list we've ever returned stays
# alive in the cache forever. The cache size is bounded by the number of
# distinct database-set states encountered, which is small in practice.
_db_items_cache = {}


def database_enum_items(self, context):
    """EnumProperty items() callback. (none) is always first."""
    names = tuple(list_databases())
    cached = _db_items_cache.get(names)
    if cached is not None:
        return cached
    items = [("", "(none)", "Don't auto-apply any material database")]
    for name in names:
        items.append((name, name, "Material database: " + name))
    _db_items_cache[names] = items
    return items


def _validate_active_matdb_deferred():
    """One-shot timer callback that runs after Blender is fully initialized.
    Resets prefs.active_matdb to "" if it points at a database file that no
    longer exists. Silences the spurious 'matches no enum' warning that
    fires when the stored value doesn't appear in the current items list."""
    try:
        # __package__ here is the addon's package name (e.g. 'step_importer').
        addon = bpy.context.preferences.addons.get(__package__)
        if addon is None:
            return None  # don't reschedule
        prefs = addon.preferences
        cur = prefs.active_matdb
        if cur and cur not in list_databases():
            try:
                prefs.active_matdb = ""
            except TypeError:
                pass
    except Exception:
        pass
    return None  # one-shot


# ── Per-object STEP material recording ──────────────────────────────────────

def stamp_step_materials(obj):
    """Record the current material slot names on the object so future DB
    application has a stable original-name reference."""
    if obj.type != "MESH" or obj.data is None:
        return
    names = [m.name if m else "" for m in obj.data.materials]
    obj[_OBJ_PROP] = json.dumps(names)


def get_recorded_originals(obj):
    raw = obj.get(_OBJ_PROP)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ── DB read / write ──────────────────────────────────────────────────────────

def _write_database(path, mappings):
    """Save mappings + referenced materials to a .blend at `path`."""
    # Make / refresh the JSON text datablock.
    txt = bpy.data.texts.get(_DB_TEXT_NAME)
    if txt is None:
        txt = bpy.data.texts.new(_DB_TEXT_NAME)
    txt.clear()
    txt.write(json.dumps(mappings, indent=2, sort_keys=True))

    datablocks = {txt}
    for repl_name in set(mappings.values()):
        if not repl_name:
            continue
        mat = bpy.data.materials.get(repl_name)
        if mat is not None:
            datablocks.add(mat)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.data.libraries.write(path, datablocks, fake_user=True)


def _read_database_mappings(path):
    """Return mappings dict from a .blend's JSON text datablock."""
    if not os.path.isfile(path):
        return {}
    with bpy.data.libraries.load(path, link=False) as (data_from, data_to):
        if _DB_TEXT_NAME in data_from.texts:
            data_to.texts = [_DB_TEXT_NAME]
        else:
            data_to.texts = []

    mappings = {}
    txt = bpy.data.texts.get(_DB_TEXT_NAME)
    if txt is not None:
        try:
            mappings = json.loads(txt.as_string())
        except Exception:
            mappings = {}
        # The appended text is a one-shot read; unlink it after parsing so we
        # don't pollute the user's text editor with stale DB blobs.
        bpy.data.texts.remove(txt, do_unlink=True)
    return mappings


def _append_database_materials(path, wanted_names):
    """Append any of `wanted_names` materials that aren't already present."""
    if not os.path.isfile(path) or not wanted_names:
        return 0
    needed = [n for n in wanted_names if n and bpy.data.materials.get(n) is None]
    if not needed:
        return 0
    appended = 0
    with bpy.data.libraries.load(path, link=False) as (data_from, data_to):
        present = [n for n in needed if n in data_from.materials]
        data_to.materials = present
        appended = len(present)
    # Set fake_user so they survive across saves of the user's .blend.
    for name in needed:
        m = bpy.data.materials.get(name)
        if m is not None:
            m.use_fake_user = True
    return appended


def ensure_database_loaded(name):
    """Read mappings + append materials for the given DB name. Returns the
    mappings dict (may be empty)."""
    if not name:
        return {}
    path = _db_path(name)
    mappings = _read_database_mappings(path)
    _append_database_materials(path, set(mappings.values()))
    return mappings


# ── Apply ────────────────────────────────────────────────────────────────────

def apply_to_objects(objects, mappings):
    """Walk material slots and swap any matching the mappings. Returns the
    number of slot replacements made."""
    if not mappings:
        return 0
    replaced = 0
    seen_meshes = set()
    for obj in objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        if obj.data in seen_meshes:
            continue
        seen_meshes.add(obj.data)

        originals = get_recorded_originals(obj)
        slots = obj.data.materials
        for i in range(len(slots)):
            if originals and i < len(originals):
                orig = originals[i]
            else:
                cur = slots[i]
                orig = cur.name if cur else ""
            if not orig:
                continue
            repl_name = mappings.get(orig)
            if not repl_name:
                continue
            repl_mat = bpy.data.materials.get(repl_name)
            if repl_mat is None or slots[i] is repl_mat:
                continue
            slots[i] = repl_mat
            replaced += 1
    return replaced


# ── Scene scan ───────────────────────────────────────────────────────────────

def scan_scene_mappings():
    """Build a {original → most-common-replacement} dict from all objects in
    the file that carry STEP_materials."""
    counts = {}  # {orig: Counter({repl_name: n})}
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        originals = get_recorded_originals(obj)
        if not originals:
            continue
        for i, orig in enumerate(originals):
            if not orig or i >= len(obj.data.materials):
                continue
            cur = obj.data.materials[i]
            cur_name = cur.name if cur else ""
            if not cur_name or cur_name == orig:
                continue
            counts.setdefault(orig, Counter())[cur_name] += 1
    return {o: c.most_common(1)[0][0] for o, c in counts.items() if c}


def scan_scene_originals():
    """All distinct original STEP material names in the file."""
    names = set()
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        originals = get_recorded_originals(obj)
        if not originals:
            continue
        for n in originals:
            if n:
                names.add(n)
    return sorted(names)


# ── Property groups + UI list ────────────────────────────────────────────────

class STEP_MatDB_Mapping(bpy.types.PropertyGroup):
    original_name: StringProperty(name="Original")
    replacement_name: StringProperty(name="Replacement")


class STEP_MatDB_SceneProps(bpy.types.PropertyGroup):
    mappings: CollectionProperty(type=STEP_MatDB_Mapping)
    active_index: IntProperty(default=0)
    selection_only: BoolProperty(
        name="Selection only",
        description="Apply mappings only to selected objects (otherwise all)",
        default=False,
    )


class STEP_UL_MatDB_Mappings(bpy.types.UIList):
    bl_idname = "STEP_UL_matdb_mappings"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        split = layout.split(factor=0.45, align=True)
        split.label(text=item.original_name or "<empty>", icon="MATERIAL")
        split.prop_search(item, "replacement_name", bpy.data, "materials", text="")


# ── UI helpers ───────────────────────────────────────────────────────────────

def _populate_ui_from_mappings(scene_props, mappings):
    scene_props.mappings.clear()
    for orig in sorted(mappings.keys()):
        row = scene_props.mappings.add()
        row.original_name = orig
        row.replacement_name = mappings[orig] or ""


def _ui_mappings_to_dict(scene_props):
    out = {}
    for row in scene_props.mappings:
        if row.original_name:
            out[row.original_name] = row.replacement_name or ""
    return out


def _get_active_db(context):
    prefs = context.preferences.addons[__package__].preferences
    return prefs.active_matdb or ""


def _set_active_db(context, name):
    prefs = context.preferences.addons[__package__].preferences
    # Force the items callback to re-evaluate against the current filesystem
    # before assignment, then guard against the rare case where the enum
    # cache is briefly empty (can happen mid-delete on some Blender builds).
    database_enum_items(prefs, context)
    try:
        prefs.active_matdb = name or ""
    except TypeError:
        pass


# ── Operators ────────────────────────────────────────────────────────────────

class STEP_MATDB_OT_new(bpy.types.Operator):
    """Scan the current scene for STEP material assignments and save them as a new database"""
    bl_idname = "step_matdb.new"
    bl_label = "New Material Database"
    bl_description = "Create a new, empty material database"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Database Name", default="my_materials")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        clean = "".join(c for c in self.name if c.isalnum() or c in "._- ").strip()
        if not clean:
            self.report({"ERROR"}, "Please supply a valid database name.")
            return {"CANCELLED"}
        path = _db_path(clean)
        mappings = scan_scene_mappings()
        if not mappings:
            # Allow creating an empty DB seeded with originals from the scene.
            for orig in scan_scene_originals():
                mappings[orig] = ""
        if not mappings:
            self.report(
                {"WARNING"},
                "No STEP material data found in scene. Import a STEP file first.",
            )
            return {"CANCELLED"}
        _write_database(path, mappings)
        _set_active_db(context, clean)
        _populate_ui_from_mappings(context.scene.step_matdb, mappings)
        self.report({"INFO"}, "Saved %d mappings → %s" % (len(mappings), clean))
        return {"FINISHED"}


class STEP_MATDB_OT_duplicate(bpy.types.Operator):
    """Duplicate the active database under a new name"""
    bl_idname = "step_matdb.duplicate"
    bl_label = "Duplicate Material Database"
    bl_description = "Copy the current database to a new file"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="New Name", default="")

    def invoke(self, context, event):
        active = _get_active_db(context)
        if not active:
            self.report({"ERROR"}, "No active database to duplicate.")
            return {"CANCELLED"}
        self.name = active + "_copy"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        src = _db_path(_get_active_db(context))
        dst = _db_path(self.name)
        if not os.path.isfile(src):
            self.report({"ERROR"}, "Active database file not found.")
            return {"CANCELLED"}
        if os.path.exists(dst):
            self.report({"ERROR"}, "Target name already exists.")
            return {"CANCELLED"}
        shutil.copyfile(src, dst)
        _set_active_db(context, self.name)
        self.report({"INFO"}, "Duplicated → " + self.name)
        return {"FINISHED"}


class STEP_MATDB_OT_load(bpy.types.Operator):
    """Load mappings from the active database into the panel + append its materials"""
    bl_idname = "step_matdb.load"
    bl_label = "Load Material Database"
    bl_description = "Append the active database's materials and refresh its mappings into the scene"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        name = _get_active_db(context)
        if not name:
            self.report({"ERROR"}, "No active database selected.")
            return {"CANCELLED"}
        mappings = ensure_database_loaded(name)
        _populate_ui_from_mappings(context.scene.step_matdb, mappings)
        self.report({"INFO"}, "Loaded %d mappings from %s" % (len(mappings), name))
        return {"FINISHED"}


class STEP_MATDB_OT_delete(bpy.types.Operator):
    """Delete the active database file (cannot be undone)"""
    bl_idname = "step_matdb.delete"
    bl_label = "Delete Material Database"
    bl_description = "Permanently delete the active database file from disk"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        name = _get_active_db(context)
        if not name:
            return {"CANCELLED"}
        path = _db_path(name)
        if os.path.isfile(path):
            os.remove(path)
        _set_active_db(context, "")
        context.scene.step_matdb.mappings.clear()
        self.report({"INFO"}, "Deleted database " + name)
        return {"FINISHED"}


class STEP_MATDB_OT_update(bpy.types.Operator):
    """Add any new STEP material names found in the scene to the mapping list (does not save)"""
    bl_idname = "step_matdb.update"
    bl_label = "Update From Scene"
    bl_description = "Sync the mapping list with current scene material assignments"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene_props = context.scene.step_matdb
        existing = {r.original_name for r in scene_props.mappings}
        added = 0
        for orig in scan_scene_originals():
            if orig in existing:
                continue
            row = scene_props.mappings.add()
            row.original_name = orig
            row.replacement_name = ""
            added += 1
        self.report({"INFO"}, "Added %d new originals." % added)
        return {"FINISHED"}


class STEP_MATDB_OT_save(bpy.types.Operator):
    """Save the current panel mappings + referenced materials to the active database file"""
    bl_idname = "step_matdb.save"
    bl_label = "Save Material Database"
    bl_description = "Write the current mappings back into the database file on disk"
    bl_options = {"REGISTER"}

    def execute(self, context):
        name = _get_active_db(context)
        if not name:
            self.report({"ERROR"}, "No active database. Use New first.")
            return {"CANCELLED"}
        mappings = _ui_mappings_to_dict(context.scene.step_matdb)
        _write_database(_db_path(name), mappings)
        self.report({"INFO"}, "Saved %d mappings → %s" % (len(mappings), name))
        return {"FINISHED"}


class STEP_MATDB_OT_apply(bpy.types.Operator):
    """Apply the current panel mappings to scene objects"""
    bl_idname = "step_matdb.apply"
    bl_label = "Apply Material Database"
    bl_description = "Apply the active database's mappings to swap materials on imported objects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene_props = context.scene.step_matdb
        mappings = _ui_mappings_to_dict(scene_props)
        if not mappings:
            self.report({"WARNING"}, "No mappings to apply.")
            return {"CANCELLED"}
        # Make sure replacement materials are actually present.
        name = _get_active_db(context)
        if name:
            _append_database_materials(_db_path(name), set(mappings.values()))
        if scene_props.selection_only:
            objs = list(context.selected_objects)
        else:
            objs = list(bpy.data.objects)
        n = apply_to_objects(objs, mappings)
        self.report({"INFO"}, "Replaced %d slot(s)." % n)
        return {"FINISHED"}


# ── Sidebar panel ────────────────────────────────────────────────────────────

class VIEW3D_PT_step_matdb(bpy.types.Panel):
    bl_label = "Material DB"
    bl_idname = "VIEW3D_PT_step_matdb"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Step 2 Blend"

    def draw(self, context):
        layout = self.layout
        prefs = context.preferences.addons[__package__].preferences
        scene_props = context.scene.step_matdb

        # Database selector
        row = layout.row(align=True)
        row.prop(prefs, "active_matdb", text="")
        row.operator("step_matdb.delete", text="", icon="TRASH")

        row = layout.row(align=True)
        row.operator("step_matdb.new", text="New", icon="ADD")
        row.operator("step_matdb.duplicate", text="Duplicate", icon="DUPLICATE")
        row.operator("step_matdb.load", text="Load", icon="IMPORT")

        layout.separator()

        # Mapping list
        layout.label(text="Mappings (%d)" % len(scene_props.mappings))
        layout.template_list(
            "STEP_UL_matdb_mappings", "",
            scene_props, "mappings",
            scene_props, "active_index",
            rows=8,
        )

        row = layout.row(align=True)
        row.operator("step_matdb.update", text="Update", icon="FILE_REFRESH")
        row.operator("step_matdb.save", text="Save", icon="FILE_TICK")

        row = layout.row(align=True)
        row.prop(scene_props, "selection_only")
        row.operator("step_matdb.apply", text="Apply", icon="CHECKMARK")


# ── Registration ─────────────────────────────────────────────────────────────

_classes = (
    STEP_MatDB_Mapping,
    STEP_MatDB_SceneProps,
    STEP_UL_MatDB_Mappings,
    STEP_MATDB_OT_new,
    STEP_MATDB_OT_duplicate,
    STEP_MATDB_OT_load,
    STEP_MATDB_OT_delete,
    STEP_MATDB_OT_update,
    STEP_MATDB_OT_save,
    STEP_MATDB_OT_apply,
    VIEW3D_PT_step_matdb,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.step_matdb = PointerProperty(type=STEP_MatDB_SceneProps)
    # Defer the "validate active_matdb" sweep until Blender finishes startup,
    # so addon preferences are fully accessible. Fires once, ~0.5s after
    # register, then unregisters itself.
    try:
        bpy.app.timers.register(
            _validate_active_matdb_deferred,
            first_interval=0.5,
            persistent=False,
        )
    except Exception:
        pass


def unregister():
    if hasattr(bpy.types.Scene, "step_matdb"):
        del bpy.types.Scene.step_matdb
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
