"""Extract Animation Cache USD - Point Cache Export.

Exports animated geometry as a USD point cache file.
The geometry is deformed by the rig/animation, and we export the final
deformed mesh with animated point positions (no rig structure).

Outputs:
1. Point Cache USD file: Contains only the deformed geometry with animated points
   - Shape/mesh with time-sampled point positions
   - No rig structure, no control curves
   - Ready to be composed as an override in the shot

Multi-asset support:
When the instance contains members from multiple assets (namespaces),
the collector provides ``allAssetPrimPaths`` — a dict mapping each
namespace to its USD prim path in the shot stage.  The extractor
remaps *all* matched assets into a single output layer so that the
cache covers every selected character.

Hierarchy matching:
The remapper uses a three-level strategy to find each asset subtree
in the exported USD:
1. **Name match** — prim name equals the asset name from the target path
2. **Suffix match** — prim name ends with ``:asset_name`` (namespaced)
3. **Geometry root** — finds the deepest common ancestor of all Mesh
   prims in the layer (fallback when namespace stripping causes the
   prim names to differ from the USD stage names)
"""

import os

from ayon_core.pipeline import PublishValidationError
from ayon_maya.api import plugin
from ayon_maya.api.lib import maintained_selection
from maya import cmds
from pxr import Sdf


def parse_version(version_str):
    """Parse string like '0.21.0' to (0, 21, 0)"""
    return tuple(int(v) for v in version_str.split("."))


class ExtractAnimationCacheUsd(plugin.MayaExtractorPlugin):
    """Extract animation cache as USD point cache."""

    label = "Extract Animation Cache USD"
    families = ["animationCacheUsd"]
    hosts = ["maya"]
    scene_type = "usd"

    def process(self, instance):
        """Process the animation cache USD extraction.

        Steps:
        1. Export selected geometry with animation (as point cache)
        2. Remap hierarchy + apply resetXformStack (single Sdf pass)
        3. Generate representation
        """

        staging_dir = self.staging_dir(instance)

        # 1. Export animation cache USD (point cache)
        self.log.info("Exporting animation cache USD (point cache)...")
        cache_file = self._export_animation_cache(instance, staging_dir)

        # 2. Remap hierarchy to match original asset prim path(s)
        creator_attrs = instance.data.get("creator_attributes", {})
        apply_reset = creator_attrs.get("resetXformStack", True)
        self._remap_to_asset_hierarchy(
            cache_file, instance, reset_xform_stack=apply_reset
        )

        cache_filename = os.path.basename(cache_file)

        # 3. Add representation
        if "representations" not in instance.data:
            instance.data["representations"] = []

        instance.data["representations"].append({
            "name": "usd",
            "ext": "usd",
            "files": cache_filename,
            "stagingDir": staging_dir
        })

        self.log.info(f"Extracted point cache: {cache_filename}")

    def _export_animation_cache(self, instance, staging_dir) -> str:
        """Export animated geometry as USD point cache.

        Args:
            instance: Publish instance
            staging_dir: Staging directory for output

        Returns:
            str: Path to exported USD file
        """

        cmds.loadPlugin("mayaUsdPlugin", quiet=True)

        filename = f"{instance.name}_cache.usd"
        filepath = os.path.join(staging_dir, filename).replace("\\", "/")

        creator_attrs = instance.data.get("creator_attributes", {})
        sampling_mode = instance.data.get("samplingMode", "sparse")
        custom_step = instance.data.get("customStepSize", 1.0)

        frame_step = 1.0
        if sampling_mode == "custom":
            frame_step = custom_step

        members = instance.data.get("setMembers", [])
        if not members:
            raise PublishValidationError(
                f"No members to export for {instance.name}"
            )

        self.log.info(f"Exporting point cache for {len(members)} members")
        self.log.debug(f"Members: {members}")
        self.log.debug(
            f"Frame range: {instance.data.get('frameStart', 1)}"
            f"-{instance.data.get('frameEnd', 1)}"
        )
        self.log.debug(f"Sampling: {sampling_mode} (step: {frame_step})")

        options = {
            "file": filepath,
            "selection": True,
            "frameRange": (
                instance.data.get("frameStart", 1),
                instance.data.get("frameEnd", 1)
            ),
            "frameStride": frame_step,
            "exportSkels": "none",
            "exportSkin": "none",
            "exportBlendShapes": False,
            "stripNamespaces": creator_attrs.get("stripNamespaces", True),
            "mergeTransformAndShape": True,
            "exportDisplayColor": False,
            "exportVisibility": False,
            "exportColorSets": False,
            "exportUVs": True,
            "exportInstances": False,
            "defaultUSDFormat": "usdc",
            "staticSingleSample": False,
            "eulerFilter": True,
        }

        # Try worldspace if available (Maya USD 0.21.0+)
        has_worldspace = False
        try:
            maya_usd_version = parse_version(
                cmds.pluginInfo("mayaUsdPlugin", query=True, version=True)
            )
            if maya_usd_version >= (0, 21, 0):
                options["worldspace"] = True
                has_worldspace = True
        except Exception as e:
            self.log.debug(f"Could not determine Maya USD version: {e}")

        self.log.debug(f"Export options: {options}")

        fallbacks = [{}]
        if has_worldspace:
            fallbacks.append({"worldspace": False})

        with maintained_selection():
            cmds.select(members, replace=True, noExpand=True)
            last_error = None
            for i, overrides in enumerate(fallbacks):
                attempt_opts = dict(options, **overrides)
                if overrides.get("worldspace") is False:
                    attempt_opts.pop("worldspace", None)
                try:
                    if i > 0:
                        self.log.warning(
                            f"Retrying export (attempt {i + 1}) "
                            f"with overrides: {overrides}"
                        )
                    cmds.mayaUSDExport(**attempt_opts)
                    last_error = None
                    break
                except RuntimeError as e:
                    last_error = e
                    self.log.warning(f"Export attempt {i + 1} failed: {e}")

            if last_error is not None:
                raise PublishValidationError(
                    f"Failed to export USD animation cache after "
                    f"{len(fallbacks)} attempt(s): {last_error}\n\n"
                    f"This can happen with complex rigs. Try:\n"
                    f"- Selecting only the geometry group (not the rig root)\n"
                    f"- Checking for invalid mesh topology\n"
                    f"- Ensuring all deformers evaluate correctly"
                )

        if not os.path.exists(filepath):
            raise PublishValidationError(
                f"USD export failed, file not created: {filepath}"
            )

        self.log.debug(f"Exported point cache USD: {filepath}")
        return filepath

    # ------------------------------------------------------------------
    # Hierarchy remapping + resetXformStack
    # ------------------------------------------------------------------

    def _remap_to_asset_hierarchy(self, filepath, instance,
                                  reset_xform_stack=False):
        """Remap exported USD hierarchy to match original asset prim paths.

        Supports **multiple assets** in a single export.  Uses a
        three-level matching strategy:

        1. **Name match** — exact prim name match
        2. **Suffix match** — namespace-prefixed name (``ns:name``)
        3. **Geometry root** — deepest common ancestor of all Mesh
           prims (fallback when ``stripNamespaces`` causes names to
           diverge from the USD stage prim names)

        Args:
            filepath: Path to the exported USD file.
            instance: Publish instance.
            reset_xform_stack: If True, add ``!resetXformStack!``.
        """

        all_paths = instance.data.get("allAssetPrimPaths", {})
        if not all_paths:
            single = instance.data.get("originalAssetPrimPath", "")
            if single:
                all_paths = {"_single": single}

        if not all_paths:
            self.log.warning(
                "No originalAssetPrimPath available. "
                "Cannot remap hierarchy."
            )
            return

        layer = Sdf.Layer.FindOrOpen(filepath)
        if not layer:
            self.log.error(f"Could not open exported USD: {filepath}")
            return

        # Log the exported layer structure for debugging
        self._log_layer_structure(layer)

        # Build new layer with correct hierarchy
        new_layer = Sdf.Layer.CreateAnonymous()
        self._copy_layer_metadata(layer, new_layer)

        first_root_name = None
        remapped_count = 0
        num_targets = len(all_paths)

        for ns, target_str in all_paths.items():
            target_path = Sdf.Path(target_str)
            asset_name = target_path.name

            # Strategy 1: exact name match
            source_path = self._find_prim_by_name(layer, asset_name)

            # Strategy 2: namespace-suffixed match
            if not source_path:
                source_path = self._find_prim_by_name_suffix(
                    layer, asset_name
                )

            # Strategy 3: geometry root fallback — when there is
            # exactly one target, use the geometry root of the whole
            # exported layer regardless of its prim name.
            if not source_path and num_targets == 1:
                source_path = self._find_geometry_root(layer)
                if source_path:
                    self.log.info(
                        f"Name match failed for '{asset_name}'. "
                        f"Using geometry root: {source_path}"
                    )

            if not source_path:
                self.log.warning(
                    f"Could not find prim for asset '{asset_name}' "
                    f"(namespace '{ns}') in exported USD. "
                    f"Skipping this asset."
                )
                continue

            # Check if already at correct path
            if source_path == target_path:
                self.log.debug(
                    f"Asset '{asset_name}' already at correct path"
                )
                # Sanitise in-place on the source layer
                self._cleanup_non_geometry(layer, target_path)
                self._strip_material_bindings(layer, target_path)
                self._convert_to_over_specifiers(layer, target_path)
                if reset_xform_stack:
                    self._apply_reset_xform_stack_sdf(layer, target_path)
                # Copy from sanitised source into new layer
                prefixes = target_path.GetPrefixes()
                for prefix in prefixes[:-1]:
                    if not new_layer.GetPrimAtPath(prefix):
                        ps = Sdf.CreatePrimInLayer(new_layer, prefix)
                        ps.specifier = Sdf.SpecifierOver
                        ps.typeName = "Xform"
                Sdf.CopySpec(layer, target_path, new_layer, target_path)
                if first_root_name is None:
                    first_root_name = prefixes[0].name
                remapped_count += 1
                continue

            self.log.info(
                f"Remapping '{asset_name}': {source_path} -> {target_path}"
            )

            # Create parent Xform prims with 'over' specifier
            prefixes = target_path.GetPrefixes()
            for prefix in prefixes[:-1]:
                if not new_layer.GetPrimAtPath(prefix):
                    prim_spec = Sdf.CreatePrimInLayer(new_layer, prefix)
                    prim_spec.specifier = Sdf.SpecifierOver
                    prim_spec.typeName = "Xform"

            # Copy the asset subtree
            if not Sdf.CopySpec(
                layer, source_path, new_layer, target_path
            ):
                self.log.error(
                    f"Failed to copy: {source_path} -> {target_path}"
                )
                continue

            # Sanitise this asset's subtree
            self._cleanup_non_geometry(new_layer, target_path)
            self._strip_material_bindings(new_layer, target_path)
            self._convert_to_over_specifiers(new_layer, target_path)
            if reset_xform_stack:
                self._apply_reset_xform_stack_sdf(new_layer, target_path)

            if first_root_name is None:
                first_root_name = prefixes[0].name
            remapped_count += 1

        if remapped_count == 0:
            self.log.error(
                "No assets could be remapped. The exported USD will "
                "keep its original hierarchy."
            )
            return

        if first_root_name:
            new_layer.defaultPrim = first_root_name

        new_layer.Export(filepath)
        self.log.info(
            f"Hierarchy remapped for {remapped_count} asset(s)"
        )

    # ------------------------------------------------------------------
    # Prim finding strategies
    # ------------------------------------------------------------------

    def _find_prim_by_name(self, layer, name):
        """Find first prim with exact name match (depth-first)."""

        def _search(parent_path):
            spec = layer.GetPrimAtPath(parent_path)
            if not spec:
                return None
            for child_spec in spec.nameChildren:
                child_path = parent_path.AppendChild(child_spec.name)
                if child_spec.name == name:
                    return child_path
                result = _search(child_path)
                if result:
                    return result
            return None

        for root_spec in layer.rootPrims:
            root_path = Sdf.Path.absoluteRootPath.AppendChild(root_spec.name)
            if root_spec.name == name:
                return root_path
            result = _search(root_path)
            if result:
                return result
        return None

    def _find_prim_by_name_suffix(self, layer, name):
        """Find prim whose name ends with ':name' (namespace handling)."""
        suffix = f":{name}"

        def _search(parent_path):
            spec = layer.GetPrimAtPath(parent_path)
            if not spec:
                return None
            for child_spec in spec.nameChildren:
                child_path = parent_path.AppendChild(child_spec.name)
                if child_spec.name.endswith(suffix):
                    return child_path
                result = _search(child_path)
                if result:
                    return result
            return None

        for root_spec in layer.rootPrims:
            root_path = Sdf.Path.absoluteRootPath.AppendChild(root_spec.name)
            if root_spec.name.endswith(suffix):
                return root_path
            result = _search(root_path)
            if result:
                return result
        return None

    def _find_geometry_root(self, layer):
        """Find the geometry root: deepest common ancestor of all Meshes.

        When ``stripNamespaces`` removes the namespace prefix from prim
        names, the asset name no longer appears in the exported
        hierarchy.  This fallback finds all Mesh prims, computes their
        deepest common ancestor, and returns it as the "asset root"
        to remap.

        Returns:
            Sdf.Path or None: The geometry root path.
        """
        mesh_paths = []

        def _collect(path):
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return
            if spec.typeName == "Mesh":
                mesh_paths.append(path)
            for child in spec.nameChildren:
                _collect(path.AppendChild(child.name))

        for root_spec in layer.rootPrims:
            _collect(
                Sdf.Path.absoluteRootPath.AppendChild(root_spec.name)
            )

        if not mesh_paths:
            self.log.debug("No Mesh prims found in exported USD")
            return None

        # Find deepest common ancestor
        prefix_lists = [p.GetPrefixes() for p in mesh_paths]
        min_depth = min(len(pl) for pl in prefix_lists)

        common = Sdf.Path.absoluteRootPath
        for i in range(min_depth):
            candidate = prefix_lists[0][i]
            if all(pl[i] == candidate for pl in prefix_lists):
                common = candidate
            else:
                break

        if common == Sdf.Path.absoluteRootPath:
            # No common ancestor beyond root — use the first root prim
            first_root = list(layer.rootPrims)
            if first_root:
                common = Sdf.Path.absoluteRootPath.AppendChild(
                    first_root[0].name
                )

        self.log.debug(
            f"Geometry root: {common} "
            f"(from {len(mesh_paths)} mesh prim(s))"
        )
        return common

    def _log_layer_structure(self, layer, max_depth=4):
        """Log the prim structure of a layer for debugging."""
        lines = []

        def _walk(path, depth=0):
            if depth >= max_depth:
                return
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return
            indent = "  " * depth
            lines.append(
                f"{indent}{spec.name} ({spec.typeName or 'untyped'})"
            )
            for child in spec.nameChildren:
                _walk(path.AppendChild(child.name), depth + 1)

        for root_spec in layer.rootPrims:
            root_path = Sdf.Path.absoluteRootPath.AppendChild(
                root_spec.name
            )
            _walk(root_path)

        if lines:
            structure = "\n".join(lines)
            self.log.debug(
                f"Exported USD structure:\n{structure}"
            )

    # ------------------------------------------------------------------
    # resetXformStack
    # ------------------------------------------------------------------

    def _apply_reset_xform_stack_sdf(self, layer, root_path):
        """Add ``!resetXformStack!`` via the Sdf API."""
        RESET_TOKEN = "!resetXformStack!"
        xformable_types = {"Xform", "Scope", "Mesh"}

        def _set_reset(prim_spec):
            if prim_spec is None:
                return False

            attr = prim_spec.attributes.get("xformOpOrder")
            if attr is not None:
                current = list(attr.default)
                if RESET_TOKEN in current:
                    return False
                attr.default = [RESET_TOKEN] + current
            else:
                attr = Sdf.AttributeSpec(
                    prim_spec,
                    "xformOpOrder",
                    Sdf.ValueTypeNames.TokenArray
                )
                attr.default = [RESET_TOKEN]

            self.log.debug(
                f"Added resetXformStack to: {prim_spec.path}"
            )
            return True

        modified = False

        root_spec = layer.GetPrimAtPath(root_path)
        if root_spec and root_spec.typeName in xformable_types:
            modified |= _set_reset(root_spec)

        if root_spec:
            for child_spec in root_spec.nameChildren:
                if child_spec.typeName in xformable_types:
                    modified |= _set_reset(child_spec)

        if modified:
            self.log.info(
                "Applied !resetXformStack! to prevent double-transforms"
            )

    # ------------------------------------------------------------------
    # Layer metadata
    # ------------------------------------------------------------------

    def _copy_layer_metadata(self, source_layer, target_layer):
        """Copy layer-level metadata (timeCode, upAxis, etc.)."""
        source_root = source_layer.pseudoRoot
        target_root = target_layer.pseudoRoot

        skip_keys = {
            "primChildren", "defaultPrim",
            "subLayers", "subLayerOffsets",
        }
        for key in source_root.ListInfoKeys():
            if key not in skip_keys:
                try:
                    target_root.SetInfo(key, source_root.GetInfo(key))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Override-layer sanitisation
    # ------------------------------------------------------------------

    def _strip_material_bindings(self, layer, root_path):
        """Remove all material-related opinions from the cache layer."""
        stripped = 0

        def _strip(path):
            nonlocal stripped
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return

            rels_to_remove = [
                rel.name for rel in spec.relationships
                if rel.name.startswith("material:binding")
            ]
            for name in rels_to_remove:
                spec.RemoveProperty(spec.relationships[name])
                stripped += 1

            attrs_to_remove = [
                attr.name for attr in spec.attributes
                if attr.name.startswith("subsetFamily:")
            ]
            for name in attrs_to_remove:
                spec.RemoveProperty(spec.attributes[name])
                stripped += 1

            api_attr = spec.GetInfo("apiSchemas")
            if api_attr:
                cleaned = [
                    s for s in api_attr.GetAddedOrExplicitItems()
                    if s != "MaterialBindingAPI"
                ]
                if len(cleaned) < len(api_attr.GetAddedOrExplicitItems()):
                    if cleaned:
                        spec.SetInfo(
                            "apiSchemas",
                            Sdf.TokenListOp.CreateExplicit(cleaned),
                        )
                    else:
                        spec.ClearInfo("apiSchemas")
                    stripped += 1

            for child_spec in spec.nameChildren:
                _strip(path.AppendChild(child_spec.name))

        _strip(root_path)
        if stripped:
            self.log.debug(
                f"Stripped {stripped} material-related opinion(s)"
            )

    def _convert_to_over_specifiers(self, layer, root_path):
        """Change ``def`` specifiers to ``over`` on all prims."""
        for prefix in root_path.GetPrefixes():
            spec = layer.GetPrimAtPath(prefix)
            if spec and spec.specifier == Sdf.SpecifierDef:
                spec.specifier = Sdf.SpecifierOver

        def _convert(path):
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return
            if spec.specifier == Sdf.SpecifierDef:
                spec.specifier = Sdf.SpecifierOver
            for child_spec in spec.nameChildren:
                _convert(path.AppendChild(child_spec.name))

        _convert(root_path)

    # ------------------------------------------------------------------
    # Non-geometry cleanup
    # ------------------------------------------------------------------

    def _cleanup_non_geometry(self, layer, root_path):
        """Remove non-geometry prims from the pointcache."""
        non_geo_types = {
            "BasisCurves", "Material", "Shader",
            "NodeGraph", "MayaReference", "GeomSubset",
        }

        prims_to_remove = []

        def _collect_non_geo(path):
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return
            if spec.typeName in non_geo_types:
                prims_to_remove.append(path)
                return
            for child_spec in list(spec.nameChildren):
                _collect_non_geo(path.AppendChild(child_spec.name))

        _collect_non_geo(root_path)

        if prims_to_remove:
            edit = Sdf.BatchNamespaceEdit()
            for path in reversed(prims_to_remove):
                edit.Add(path, Sdf.Path.emptyPath)
            layer.Apply(edit)
            self.log.debug(
                f"Removed {len(prims_to_remove)} non-geometry prims"
            )

        self._remove_empty_containers(layer, root_path)

    def _remove_empty_containers(self, layer, root_path):
        """Remove Xform/Scope prims that have no geometry descendants."""
        geo_types = {
            "Mesh", "Points",
            "NurbsPatch", "PointInstancer",
        }

        def _has_geometry(path):
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return False
            if spec.typeName in geo_types:
                return True
            for child_spec in spec.nameChildren:
                if _has_geometry(path.AppendChild(child_spec.name)):
                    return True
            return False

        def _collect_empty(path):
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return []
            empties = []
            for child_spec in list(spec.nameChildren):
                child_path = path.AppendChild(child_spec.name)
                child_obj = layer.GetPrimAtPath(child_path)
                if (child_obj
                        and child_obj.typeName in ("Xform", "Scope")
                        and not _has_geometry(child_path)):
                    empties.append(child_path)
                else:
                    empties.extend(_collect_empty(child_path))
            return empties

        empties = _collect_empty(root_path)
        if empties:
            edit = Sdf.BatchNamespaceEdit()
            for path in reversed(empties):
                edit.Add(path, Sdf.Path.emptyPath)
            layer.Apply(edit)
            self.log.debug(
                f"Removed {len(empties)} empty containers"
            )
