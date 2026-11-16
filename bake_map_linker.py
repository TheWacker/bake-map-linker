"""
Bake Map Linker - Substance 3D Painter plugin
==============================================
Version 1
Created by Wacker (aka Amit Kumar Sharma)

Imports externally baked mesh maps (Marmoset, Blender, Maya, Painter's own
exports, etc.) from a folder and assigns them to the matching slots in
Texture Set Settings > Mesh Maps. A single "Update Maps" button re-imports
any maps that were re-baked on disk, so there's no need to delete and reassign them.

Install:
    Copy this file into
      Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
    then in Painter use  Python > Reload Plugins Folder  and enable
    "bake_map_linker" in the Python menu. The panel appears under
    Window > Views > Bake Map Linker.

Requires Substance 3D Painter 7.2 or newer (mesh map Python API).
Works with PySide2 (Painter <= 9) and PySide6 (Painter 10+).
"""

import json
import os
import re

import substance_painter.event as sp_event
import substance_painter.logging as sp_log
import substance_painter.project as sp_project
import substance_painter.resource as sp_res
import substance_painter.textureset as sp_ts
import substance_painter.ui as sp_ui

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # Painter 9 and older
    from PySide2 import QtCore, QtGui, QtWidgets


PLUGIN_NAME = "Bake Map Linker"
PLUGIN_VERSION = "1.0"
PLUGIN_AUTHOR = "Wacker (Amit Kumar Sharma)"
METADATA_ID = "BakeMapLinker"
DEBOUNCE_MS = 2000  # wait for bakers to finish writing before updating

IMAGE_EXTS = {
    ".png", ".tga", ".tif", ".tiff", ".exr", ".jpg", ".jpeg",
    ".psd", ".bmp", ".hdr", ".dds", ".webp",
}

MMU = sp_ts.MeshMapUsage

# (MeshMapUsage attribute name, UI label, filename patterns)
# Order matters: on a tie, the earlier rule wins, so the more specific
# rules (world space / bent normals) come before the plain Normal rule.
_RULES_RAW = [
    ("WorldSpaceNormal", "World Space Normal", [
        r"world_space_normals?", r"worldspace_normals?", r"worldspacenormals?",
        r"world_normals?", r"worldnormals?", r"ws_normals?", r"normals?_ws",
        r"wsn", r"object_space_normals?", r"normals?_os", r"os_normals?",
        r"normals?_?obj(?:ect)?", r"obj(?:ect)?_?normals?",
        r"normals?_?world", r"normals?_?ws", r"normals?_?os",
    ]),
    ("BentNormals", "Bent Normals", [
        r"bent_normals?", r"bentnormals?", r"bn",
    ]),
    ("Normal", "Normal", [
        r"normal_base", r"tangent_space_normals?", r"tangent_normals?",
        r"tangentnormals?", r"normals?_ts", r"normal_?map", r"normals?",
        r"nrm", r"nor", r"norm", r"nml", r"nrml",
    ]),
    ("AO", "Ambient Occlusion", [
        r"ambient_occlusion", r"ambientocclusion", r"occlusion", r"ao",
    ]),
    ("Curvature", "Curvature", [r"curvature", r"curves?", r"curv", r"crv"]),
    ("Position", "Position", [r"position", r"pos"]),
    ("Thickness", "Thickness", [r"thickness", r"thick"]),
    ("Height", "Height", [r"height_?map", r"height", r"displacement", r"disp"]),
    ("Opacity", "Opacity", [r"opacity", r"alpha"]),
    ("ID", "ID", [
        r"color_id", r"colorid", r"colour_id", r"material_id", r"materialid",
        r"mat_id", r"matid", r"object_id", r"objectid", r"vertex_color", r"id",
    ]),
]

# Only keep slots that exist in this Painter version
RULES = []
for _name, _label, _pats in _RULES_RAW:
    _usage = getattr(MMU, _name, None)
    if _usage is not None:
        _rx = re.compile(r"(?:^|(?<=_))(?:" + "|".join(_pats) + r")(?=_|$)")
        RULES.append((_name, _label, _usage, _rx))

SLOT_LABELS = {name: label for name, label, _u, _r in RULES}
SKIP = "(skip)"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _norm(text):
    """Lower-case and turn every separator into a single underscore."""
    return re.sub(r"[\s\-.]+", "_", text.lower()).strip("_")


def _file_sig(path):
    try:
        st = os.stat(path)
        return [int(st.st_mtime_ns), int(st.st_size)]
    except OSError:
        return None


def _run_when_idle(fn):
    """Run fn now, or once Painter has finished a long operation."""
    is_busy = getattr(sp_project, "is_busy", None)
    later = getattr(sp_project, "execute_when_not_busy", None)
    if is_busy and later and is_busy():
        later(fn)
    else:
        fn()


def match_texture_set(norm_name, ts_names):
    """Return (texture_set_name, remainder) - the longest name found in file."""
    best, best_len, remainder = None, -1, norm_name
    for ts in ts_names:
        t = _norm(ts)
        if not t:
            continue
        rx = re.compile(r"(?:^|(?<=_))" + re.escape(t) + r"(?=_|$)")
        m = rx.search(norm_name)
        if m and len(t) > best_len:
            best, best_len = ts, len(t)
            remainder = (norm_name[:m.start()] + "_" + norm_name[m.end():]).strip("_")
            remainder = re.sub(r"_+", "_", remainder)
    return best, remainder


def detect_slot(norm_name):
    """Return the MeshMapUsage name whose keyword appears latest in the name.

    Map type is almost always the suffix (Body_normal, Body_ao_1001 ...), so the
    match ending furthest right wins; ties fall back to rule order.
    """
    best_name, best_end = None, -1
    for name, _label, _usage, rx in RULES:
        for m in rx.finditer(norm_name):
            if m.end() > best_end:
                best_name, best_end = name, m.end()
    return best_name


def _make_icon():
    """Sidebar icon drawn in code: a 2x2 grid of map tiles, one highlighted."""
    icon = QtGui.QIcon()
    for size in (16, 24, 32, 48, 64):
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        u = size / 24.0
        pen = QtGui.QPen(QtGui.QColor(215, 215, 215))
        pen.setWidthF(max(1.0, 1.6 * u))
        p.setPen(pen)
        tiles = [(3, 3), (13, 3), (3, 13), (13, 13)]
        for i, (x, y) in enumerate(tiles):
            rect = QtCore.QRectF(x * u, y * u, 8 * u, 8 * u)
            if i == 3:
                p.setBrush(QtGui.QColor(215, 215, 215))
            else:
                p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(rect, 1.5 * u, 1.5 * u)
        p.end()
        icon.addPixmap(pm)
    return icon


# ----------------------------------------------------------------------------
# Plugin
# ----------------------------------------------------------------------------
class BakeMapLinker(QtCore.QObject):

    def __init__(self):
        super().__init__()
        self._build_ui()
        self.dock = sp_ui.add_dock_widget(self.widget)
        # Painter lays out its panels after plugins load, so dock a moment later
        QtCore.QTimer.singleShot(1500, self._dock_into_sidebar)

        self.watcher = QtCore.QFileSystemWatcher()
        self.watcher.fileChanged.connect(self._on_fs_changed)
        self.watcher.directoryChanged.connect(self._on_fs_changed)

        self.debounce = QtCore.QTimer()
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(DEBOUNCE_MS)
        self.debounce.timeout.connect(self._on_debounced_change)

        sp_event.DISPATCHER.connect(sp_event.ProjectOpened, self._on_project_opened)
        sp_event.DISPATCHER.connect(sp_event.ProjectCreated, self._on_project_opened)
        sp_event.DISPATCHER.connect(sp_event.ProjectAboutToClose, self._on_project_closing)

        if sp_project.is_open():
            self._on_project_opened(None)
        else:
            self._set_enabled(False)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        w = QtWidgets.QWidget()
        w.setObjectName("BakeMapLinker")
        w.setWindowTitle(PLUGIN_NAME)
        w.setWindowIcon(_make_icon())  # icon shown in Painter's right sidebar
        lay = QtWidgets.QVBoxLayout(w)

        row = QtWidgets.QHBoxLayout()
        self.folder_edit = QtWidgets.QLineEdit()
        self.folder_edit.setPlaceholderText("Folder containing baked maps...")
        self.folder_edit.editingFinished.connect(self._on_folder_edited)
        browse = QtWidgets.QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        lay.addLayout(row)

        opts = QtWidgets.QHBoxLayout()
        self.recursive_cb = QtWidgets.QCheckBox("Include subfolders")
        self.recursive_cb.toggled.connect(lambda _=None: self.scan())
        self.auto_cb = QtWidgets.QCheckBox("Auto-update when files change")
        self.auto_cb.toggled.connect(self._save_settings)
        opts.addWidget(self.recursive_cb)
        opts.addWidget(self.auto_cb)
        opts.addStretch(1)
        lay.addLayout(opts)

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Use", "File", "Texture Set", "Mesh Map Slot", "Status"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        lay.addWidget(self.table, 1)

        btns = QtWidgets.QHBoxLayout()
        self.scan_btn = QtWidgets.QPushButton("Scan Folder")
        self.scan_btn.clicked.connect(self.scan)
        self.import_btn = QtWidgets.QPushButton("Import && Assign")
        self.import_btn.setToolTip("Import the checked files and plug them into their mesh map slots.")
        self.import_btn.clicked.connect(lambda: _run_when_idle(self.import_and_assign))
        self.update_btn = QtWidgets.QPushButton("Update Maps")
        self.update_btn.setToolTip(
            "Re-import every linked map whose file changed on disk and swap it\n"
            "into the same slot. Shift+click to force-update all linked maps.")
        self.update_btn.clicked.connect(self._on_update_clicked)
        btns.addWidget(self.scan_btn)
        btns.addWidget(self.import_btn)
        btns.addWidget(self.update_btn)
        lay.addLayout(btns)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(110)
        lay.addWidget(self.log_view)

        self.widget = w

    def _dock_into_sidebar(self):
        """Tab the panel in with Painter's right-hand panels so it gets a
        sidebar icon. Done on first run, and again whenever the panel is
        left floating; after that Painter remembers where you put it."""
        try:
            main = sp_ui.get_main_window()
            dock = self.dock if isinstance(self.dock, QtWidgets.QDockWidget) else None
            if dock is None:
                parent = self.widget.parentWidget()
                while parent is not None and not isinstance(parent, QtWidgets.QDockWidget):
                    parent = parent.parentWidget()
                dock = parent
            if main is None or dock is None:
                return

            settings = QtCore.QSettings("DigiTela", "BakeMapLinker")
            first_run = not settings.value("docked_once", False, type=bool)
            if not first_run and not dock.isFloating():
                return

            dock.setWindowIcon(self.widget.windowIcon())
            right = QtCore.Qt.RightDockWidgetArea
            others = [d for d in main.findChildren(QtWidgets.QDockWidget)
                      if d is not dock and not d.isFloating()
                      and main.dockWidgetArea(d) == right]
            preferred = ("Texture Set Settings", "Properties", "Shader settings",
                         "Display Settings", "Export")
            target = None
            for title in preferred:
                target = next((d for d in others if d.windowTitle().startswith(title)), None)
                if target:
                    break
            if target is None and others:
                target = others[0]

            main.addDockWidget(right, dock)  # make sure it belongs to the main window
            dock.setFloating(False)
            if target is not None:
                main.tabifyDockWidget(target, dock)
            dock.show()
            dock.raise_()
            settings.setValue("docked_once", True)
        except Exception as e:
            sp_log.warning("[%s] Could not dock panel: %s" % (PLUGIN_NAME, e))

    def _set_enabled(self, on):
        for b in (self.scan_btn, self.import_btn, self.update_btn,
                  self.folder_edit, self.table):
            b.setEnabled(on)

    def log(self, msg, warn=False):
        self.log_view.appendPlainText(("! " if warn else "") + msg)
        (sp_log.warning if warn else sp_log.info)("[%s] %s" % (PLUGIN_NAME, msg))

    # ---------------------------------------------------------- metadata
    def _meta(self):
        return sp_project.Metadata(METADATA_ID)

    def _meta_get(self, key, default):
        try:
            raw = self._meta().get(key)
            return json.loads(raw) if raw else default
        except Exception:
            return default

    def _meta_set(self, key, value):
        try:
            self._meta().set(key, json.dumps(value))
        except Exception as e:
            self.log("Could not save project metadata: %s" % e, warn=True)

    def _links(self):
        """{texture_set: {usage_name: {"path": str, "sig": [mtime_ns, size]}}}"""
        return self._meta_get("links", {})

    def _save_settings(self, *_):
        if not sp_project.is_open():
            return
        self._meta_set("settings", {
            "folder": self.folder_edit.text().strip(),
            "recursive": self.recursive_cb.isChecked(),
            "auto": self.auto_cb.isChecked(),
        })
        self._rewatch()

    # ------------------------------------------------------------ events
    def _on_project_opened(self, _evt):
        self._set_enabled(True)
        s = self._meta_get("settings", {})
        for cb, key in ((self.recursive_cb, "recursive"), (self.auto_cb, "auto")):
            cb.blockSignals(True)
            cb.setChecked(bool(s.get(key, False)))
            cb.blockSignals(False)
        self.folder_edit.setText(s.get("folder", ""))
        self._rewatch()
        if self.folder_edit.text():
            self.scan()
        n = self._refresh_update_button()
        if n:
            self.log("%d linked map(s) changed on disk since last update. "
                     "Click 'Update Maps'." % n)

    def _on_project_closing(self, _evt):
        self.debounce.stop()
        self._clear_watcher()
        self.table.setRowCount(0)
        self._set_enabled(False)
        self.update_btn.setText("Update Maps")

    def _on_fs_changed(self, _path):
        self.debounce.start()  # restart; fires once writes settle

    def _on_debounced_change(self):
        if not sp_project.is_open():
            return
        if self.auto_cb.isChecked() and self._changed_links():
            _run_when_idle(lambda: self.update_maps(force=False))
        else:
            self._refresh_update_button()
            self.scan(quiet=True)
        self._rewatch()  # files replaced by bakers drop out of the watcher

    # ---------------------------------------------------------- watcher
    def _clear_watcher(self):
        paths = self.watcher.files() + self.watcher.directories()
        if paths:
            self.watcher.removePaths(paths)

    def _rewatch(self):
        self._clear_watcher()
        if not sp_project.is_open():
            return
        paths = set()
        folder = self.folder_edit.text().strip()
        if folder and os.path.isdir(folder):
            paths.add(folder)
        for slots in self._links().values():
            for info in slots.values():
                p = info.get("path")
                if p and os.path.isfile(p):
                    paths.add(p)
                    paths.add(os.path.dirname(p))
        if paths:
            self.watcher.addPaths(sorted(paths))

    # ------------------------------------------------------------ folder
    def _browse(self):
        start = self.folder_edit.text() or os.path.expanduser("~")
        d = QtWidgets.QFileDialog.getExistingDirectory(self.widget, "Select bake folder", start)
        if d:
            self.folder_edit.setText(os.path.normpath(d))
            self._on_folder_edited()

    def _on_folder_edited(self):
        self._save_settings()
        self.scan()

    def _list_images(self, folder):
        out = []
        if self.recursive_cb.isChecked():
            for root, _dirs, files in os.walk(folder):
                out += [os.path.join(root, f) for f in files]
        else:
            out = [os.path.join(folder, f) for f in os.listdir(folder)]
        return sorted(p for p in out
                      if os.path.isfile(p) and os.path.splitext(p)[1].lower() in IMAGE_EXTS)

    # -------------------------------------------------------------- scan
    def scan(self, quiet=False):
        if not sp_project.is_open():
            return
        folder = self.folder_edit.text().strip()
        self.table.setRowCount(0)
        if not folder:
            return
        if not os.path.isdir(folder):
            if not quiet:
                self.log("Folder not found: %s" % folder, warn=True)
            return

        ts_names = [t.name() for t in sp_ts.all_texture_sets()]
        single_ts = ts_names[0] if len(ts_names) == 1 else None
        links = self._links()
        taken = set()
        n_matched = 0

        for path in self._list_images(folder):
            base = _norm(os.path.splitext(os.path.basename(path))[0])
            ts, rest = match_texture_set(base, ts_names)
            if ts is None:
                ts = single_ts
            slot = detect_slot(rest)

            use = bool(ts and slot)
            if use and (ts, slot) in taken:  # e.g. both .png and .exr exist
                use = False
            if use:
                taken.add((ts, slot))
                n_matched += 1

            status = ""
            if ts and slot:
                info = links.get(ts, {}).get(slot)
                if info and os.path.normcase(info["path"]) == os.path.normcase(path):
                    status = "Linked" if info.get("sig") == _file_sig(path) else "Changed"
            self._add_row(path, ts_names, ts, slot, use, status)

        if not quiet:
            self.log("Scanned %d image(s): %d matched automatically." %
                     (self.table.rowCount(), n_matched))

    def _add_row(self, path, ts_names, ts, slot, use, status):
        r = self.table.rowCount()
        self.table.insertRow(r)

        chk = QtWidgets.QTableWidgetItem()
        chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
        chk.setCheckState(QtCore.Qt.Checked if use else QtCore.Qt.Unchecked)
        self.table.setItem(r, 0, chk)

        f = QtWidgets.QTableWidgetItem(os.path.basename(path))
        f.setData(QtCore.Qt.UserRole, path)
        f.setToolTip(path)
        f.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
        self.table.setItem(r, 1, f)

        ts_combo = QtWidgets.QComboBox()
        ts_combo.addItem(SKIP, None)
        for n in ts_names:
            ts_combo.addItem(n, n)
        ts_combo.setCurrentIndex(max(0, ts_combo.findData(ts)) if ts else 0)
        self.table.setCellWidget(r, 2, ts_combo)

        slot_combo = QtWidgets.QComboBox()
        slot_combo.addItem(SKIP, None)
        for name, label, _u, _r in RULES:
            slot_combo.addItem(label, name)
        slot_combo.setCurrentIndex(max(0, slot_combo.findData(slot)) if slot else 0)
        self.table.setCellWidget(r, 3, slot_combo)

        # Picking a texture set + slot by hand ticks the row automatically
        def _auto_check(_i=None, item=chk, a=ts_combo, b=slot_combo):
            if a.currentData() and b.currentData():
                item.setCheckState(QtCore.Qt.Checked)
        ts_combo.currentIndexChanged.connect(_auto_check)
        slot_combo.currentIndexChanged.connect(_auto_check)

        st = QtWidgets.QTableWidgetItem(status)
        st.setFlags(QtCore.Qt.ItemIsEnabled)
        self.table.setItem(r, 4, st)

    # ------------------------------------------------------------ import
    def _import_and_link(self, ts_obj, usage_name, path):
        usage = getattr(MMU, usage_name)
        res = sp_res.import_project_resource(path, sp_res.Usage.TEXTURE)
        ts_obj.set_mesh_map_resource(usage, res.identifier())

    def import_and_assign(self):
        if not sp_project.is_open():
            return
        ts_by_name = {t.name(): t for t in sp_ts.all_texture_sets()}
        links = self._links()
        jobs, seen = [], set()

        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).checkState() != QtCore.Qt.Checked:
                continue
            path = self.table.item(r, 1).data(QtCore.Qt.UserRole)
            ts = self.table.cellWidget(r, 2).currentData()
            slot = self.table.cellWidget(r, 3).currentData()
            if not ts or not slot:
                self.log("Skipped %s (no texture set / slot chosen)." % os.path.basename(path), warn=True)
                continue
            if (ts, slot) in seen:
                self.log("Skipped %s: %s > %s already used by another file." %
                         (os.path.basename(path), ts, SLOT_LABELS[slot]), warn=True)
                continue
            seen.add((ts, slot))
            jobs.append((ts, slot, path))

        if not jobs:
            self.log("Nothing to import. Tick some rows first.", warn=True)
            return

        ok = 0
        for ts, slot, path in jobs:
            ts_obj = ts_by_name.get(ts)
            if ts_obj is None:
                self.log("Texture set '%s' no longer exists." % ts, warn=True)
                continue
            try:
                self._import_and_link(ts_obj, slot, path)
                links.setdefault(ts, {})[slot] = {"path": path, "sig": _file_sig(path)}
                ok += 1
                self.log("%s  ->  %s / %s" % (os.path.basename(path), ts, SLOT_LABELS[slot]))
            except Exception as e:
                self.log("Failed on %s: %s" % (os.path.basename(path), e), warn=True)

        self._meta_set("links", links)
        self.log("Assigned %d of %d map(s)." % (ok, len(jobs)))
        self._rewatch()
        self._refresh_update_button()
        self.scan(quiet=True)

    # ------------------------------------------------------------ update
    def _changed_links(self):
        out = []
        for ts, slots in self._links().items():
            for slot, info in slots.items():
                sig = _file_sig(info["path"])
                if sig is not None and sig != info.get("sig"):
                    out.append((ts, slot))
        return out

    def _refresh_update_button(self):
        if not sp_project.is_open():
            return 0
        n = len(self._changed_links())
        self.update_btn.setText("Update Maps (%d changed)" % n if n else "Update Maps")
        return n

    def _on_update_clicked(self):
        force = bool(QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.ShiftModifier)
        _run_when_idle(lambda: self.update_maps(force=force))

    def update_maps(self, force=False):
        """Re-import changed linked maps and swap them into the same slots."""
        if not sp_project.is_open():
            return
        links = self._links()
        if not links:
            self.log("No linked maps yet. Use 'Import & Assign' first.", warn=True)
            return

        ts_by_name = {t.name(): t for t in sp_ts.all_texture_sets()}
        updated = 0
        for ts, slots in links.items():
            ts_obj = ts_by_name.get(ts)
            if ts_obj is None:
                self.log("Texture set '%s' not found; skipping its maps." % ts, warn=True)
                continue
            for slot, info in slots.items():
                path = info["path"]
                sig = _file_sig(path)
                if sig is None:
                    self.log("Missing file: %s" % path, warn=True)
                    continue
                if not force and sig == info.get("sig"):
                    continue
                if getattr(MMU, slot, None) is None:
                    continue
                try:
                    self._import_and_link(ts_obj, slot, path)
                    info["sig"] = sig
                    updated += 1
                    self.log("Updated %s / %s  <-  %s" %
                             (ts, SLOT_LABELS.get(slot, slot), os.path.basename(path)))
                except Exception as e:
                    self.log("Update failed for %s: %s" % (os.path.basename(path), e), warn=True)

        self._meta_set("links", links)
        self.log("Update finished: %d map(s) refreshed." % updated if updated
                 else "All linked maps are already up to date.")
        self._rewatch()
        self._refresh_update_button()
        self.scan(quiet=True)

    # ---------------------------------------------------------- shutdown
    def shutdown(self):
        self.debounce.stop()
        self._clear_watcher()
        for evt, cb in ((sp_event.ProjectOpened, self._on_project_opened),
                        (sp_event.ProjectCreated, self._on_project_opened),
                        (sp_event.ProjectAboutToClose, self._on_project_closing)):
            try:
                sp_event.DISPATCHER.disconnect(evt, cb)
            except Exception:
                pass
        sp_ui.delete_ui_element(self.dock)


# ----------------------------------------------------------------------------
# Painter entry points
# ----------------------------------------------------------------------------
_PLUGIN = None


def start_plugin():
    global _PLUGIN
    _PLUGIN = BakeMapLinker()


def close_plugin():
    global _PLUGIN
    if _PLUGIN is not None:
        _PLUGIN.shutdown()
        _PLUGIN = None


if __name__ == "__main__":
    start_plugin()
