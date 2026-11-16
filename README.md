# Bake Map Linker for Substance 3D Painter (Version 1)

**Created by Wacker, aka Amit Kumar Sharma**

Bake Map Linker imports your baked mesh maps from a folder and puts each one in its Mesh Maps slot, based on the file name. If you re-bake in another program like Marmoset or Blender, one click brings the new versions in. You don't have to delete and reassign anything.

## How to use

1. **Install:** copy `bake_map_linker.py` into `Documents/Adobe/Adobe Substance 3D Painter/python/plugins/`. In Painter, choose **Python > Reload Plugins Folder**, then turn on **bake_map_linker** in the Python menu.
2. **Open the panel:** click the **Bake Map Linker** icon (a 2×2 grid of squares) in the right-hand sidebar. You can also open it from **Window > Views > Bake Map Linker**.
3. **Pick your folder:** click **Browse** and choose the folder with your baked maps. The plugin scans it and lists each map with the slot it detected.
4. **Check the list:** fix any wrong match with the drop-downs, and untick anything you don't want.
5. **Click Import & Assign.** Every map goes into its slot.
6. **After a re-bake:** click **Update Maps**, which shows how many maps changed. Only those maps are replaced. Shift+click updates every map. You can also tick **Auto-update when files change** and it happens on its own.

## Supported slots and file names

| Slot | Example file names |
|---|---|
| Normal | `_normal`, `_normal_base`, `_nrm`, `_normals` |
| World Space Normal | `_normalobj`, `_world_space_normals`, `_wsn`, `_normal_os` |
| Ambient Occlusion | `_ao`, `_ambient_occlusion`, `_occlusion` |
| Curvature | `_curve`, `_curves`, `_curvature` |
| Position | `_position`, `_pos` |
| Thickness | `_thickness` |
| ID | `_id`, `_materialID`, `_colorid`, `_objectid` |
| Height | `_height`, `_displacement` |
| Opacity | `_opacity`, `_alpha` |
| Bent Normals | `_bentnormal`, `_bent_normals` |

Files are matched to a texture set when its name appears in the file name (for example `Body_Armor_ao.png`). If the project has only one texture set, every map goes to it.

## How it saves time

- **No manual assigning:** setting up maps by hand means dragging up to 10 maps into slots, one at a time, for every texture set. The plugin does it in one click.
- **No delete-and-reassign loop:** without the plugin, every re-bake means removing the old maps, importing the new ones and slotting them again. Update Maps replaces that with a single click, or with no clicks if auto-update is on.
- **Fewer mistakes:** it never puts the tangent normal in the world space normal slot, misses a map or leaves a stale map in place.
- **Scales with the project:** a model with several texture sets and many re-bakes can take hours of slot work by hand, and the plugin cuts that to a few clicks.

## Requirements

- Adobe Substance 3D Painter 7.2 or newer

## Notes

Each update imports a new copy of the map. The old version stays in the project's assets as an unused resource, which Painter's "remove unused resources" cleanup in the Assets window will clear out.

## Credits

Created by **Wacker (Amit Kumar Sharma)**.
