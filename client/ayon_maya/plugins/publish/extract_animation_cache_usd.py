"""Extract Animation Cache USD - Point Cache Export.

Exports animated geometry as a USD point cache file.
The geometry is deformed by the rig/animation, and we export the final
deformed mesh with animated point positions (no rig structure).

Outputs:
1. Point Cache USD file: Contains only the deformed geometry with animated points
   - Shape/mesh with time-sampled point positions
   - No rig structure, no control curves
   - Ready to be composed as an override in the shot

The workflow:
1. Select the deformed geometry (typically from inside the rigged asset)
2. Export with proper options (no skeleton, no skin, no rig)
3. Post-process to remap hierarchy to match original asset prim path
4. Result: Clean point cache with correct hierarchy for sublayer composition

Usage:
- Select: /assets/character/cone_character/geo/cone_character_GEO (or similar)
- Publish with animationCacheUsd family
- Get: point_cache.usd with animated mesh at the correct prim path
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

        # 2. Remap hierarchy to match original asset prim path
        #    and optionally apply !resetXformStack! to prevent
        #    double-transforms from layout positioning.
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

        This exports the deformed geometry (shape/mesh) with animated point
        positions. No rig structure, no control curves - just the final
        animated geometry.

        Args:
            instance: Publish instance
            staging_dir: Staging directory for output

        Returns:
            str: Path to exported USD file
        """

        # Load Maya USD plugin
        cmds.loadPlugin("mayaUsdPlugin", quiet=True)

        # Prepare output file
        filename = f"{instance.name}_cache.usd"
        filepath = os.path.join(staging_dir, filename).replace("\\", "/")

        # Get animation settings
        creator_attrs = instance.data.get("creator_attributes", {})
        sampling_mode = instance.data.get("samplingMode", "sparse")
        custom_step = instance.data.get("customStepSize", 1.0)

        # Determine frame step
        frame_step = 1.0
        if sampling_mode == "custom":
            frame_step = custom_step

        # Get members to export (should be shape/mesh nodes)
        members = instance.data.get("setMembers", [])
        if not members:
            raise PublishValidationError(
                f"No members to export for {instance.name}"
            )

        self.log.info(f"Exporting point cache for: {members}")
        self.log.debug(
            f"Frame range: {instance.data.get('frameStart', 1)}"
            f"-{instance.data.get('frameEnd', 1)}"
        )
        self.log.debug(f"Sampling: {sampling_mode} (step: {frame_step})")

        # Prepare export options for POINT CACHE
        # Note: exportBlendShapes is False because for a point cache we
        # export the final deformed point positions (which already include
        # blendshape deformation).  Enabling it would try to export the
        # blendshape deformer *structure* as USD schema, which frequently
        # fails on complex rigs (especially combined with stripNamespaces
        # or selection-only exports) and is unnecessary for point caches.
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

        # Try to use worldspace if available (Maya USD 0.21.0+)
        has_worldspace = False
        try:
            maya_usd_version = parse_version(
                cmds.pluginInfo("mayaUsdPlugin", query=True, version=True)
            )
            if maya_usd_version >= (0, 21, 0):
                options["worldspace"] = True
                has_worldspace = True
            else:
                self.log.debug(
                    f"Maya USD {maya_usd_version} < 0.21.0, no worldspace"
                )
        except Exception as e:
            self.log.debug(f"Could not determine Maya USD version: {e}")

        self.log.debug(f"Export options: {options}")

        # Build fallback strategies: progressively disable options that
        # are known to cause failures on complex rigs / scenes.
        fallbacks = [{}]  # first attempt: use options as-is
        if has_worldspace:
            # worldspace can conflict with certain deformer stacks
            fallbacks.append({"worldspace": False})

        # Export USD with animation (with fallback chain)
        with maintained_selection():
            cmds.select(members, replace=True, noExpand=True)
            last_error = None
            for i, overrides in enumerate(fallbacks):
                attempt_opts = dict(options, **overrides)
                # Remove keys set to False that are flag-type
                # (worldspace=False means "don't pass it at all")
                if overrides.get("worldspace") is False:
                    attempt_opts.pop("worldspace", None)
                try:
                    if i > 0:
                        self.log.warning(
                            f"Retrying export (attempt {i + 1}) "
                            f"with overrides: {overrides}"
                        )
                        self.log.debug(
                            f"Fallback export options: {attempt_opts}"
                        )
                    cmds.mayaUSDExport(**attempt_opts)
                    if i > 0:
                        self.log.warning(
                            "Export succeeded with fallback options. "
                            "Disabled: %s", list(overrides.keys())
                        )
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
        """Remap exported USD hierarchy to match original asset prim path.

        When exporting geometry from 'Edit as Maya Data', the Maya USD
        exporter preserves the internal Maya scene hierarchy, producing
        paths like::

            /__mayaUsd__/rigParent/rig/<asset>/geo/mesh

        For the LayCache sublayer to compose correctly over the original
        asset in the shot stage, the hierarchy must match the original
        prim path, e.g.::

            /usdShot/assets/character/<asset>/geo/mesh

        This method:
        1. Finds the asset prim in the exported hierarchy by name
        2. Creates a new layer with the correct target hierarchy
        3. Copies the geometry subtree to the correct location
        4. Cleans up non-geometry prims (rig controls, materials)
        5. Optionally applies ``!resetXformStack!`` to the asset prim
           and its geometry children (to prevent double-transforms when
           the cache was exported with ``worldspace=True``)

        All operations happen on an in-memory ``Sdf.Layer`` before a
        single ``Export()`` to disk, which avoids layer-cache / mmap
        corruption issues with binary ``.usdc`` files on Windows.

        Args:
            filepath: Path to the exported USD file.
            instance: Publish instance.
            reset_xform_stack: If True, add ``!resetXformStack!`` to the
                asset root prim and its geometry children.
        """
        original_path = instance.data.get("originalAssetPrimPath", "")
        if not original_path:
            self.log.warning(
                "No originalAssetPrimPath available. "
                "Cannot remap LayCache hierarchy. The exported USD will "
                "keep the Maya scene hierarchy which may not compose "
                "correctly as a sublayer."
            )
            return

        target_path = Sdf.Path(original_path)
        asset_name = target_path.name

        layer = Sdf.Layer.FindOrOpen(filepath)
        if not layer:
            self.log.error(f"Could not open exported USD: {filepath}")
            return

        # Find the asset prim in the exported hierarchy
        source_path = self._find_prim_by_name(layer, asset_name)

        # Fallback: try namespace-suffixed match (when stripNamespaces=False)
        if not source_path:
            source_path = self._find_prim_by_name_suffix(layer, asset_name)

        if not source_path:
            self.log.warning(
                f"Could not find prim '{asset_name}' in exported USD. "
                "Hierarchy remapping skipped."
            )
            return

        if source_path == target_path:
            self.log.debug("Hierarchy already correct, no remapping needed")
            # Still need to sanitise the layer for override use
            self._cleanup_non_geometry(layer, target_path)
            self._strip_material_bindings(layer, target_path)
            self._convert_to_over_specifiers(layer, target_path)
            if reset_xform_stack:
                self._apply_reset_xform_stack_sdf(layer, target_path)
            layer.Save()
            return

        self.log.info(f"Remapping hierarchy: {source_path} -> {target_path}")

        # Build new layer with correct hierarchy
        new_layer = Sdf.Layer.CreateAnonymous()
        self._copy_layer_metadata(layer, new_layer)

        # Create parent Xform prims for the target path.
        # Use 'over' specifier — these prims already exist in the shot
        # stage; we only need to provide the hierarchy anchor, not
        # redefine them.
        prefixes = target_path.GetPrefixes()
        for prefix in prefixes[:-1]:
            if not new_layer.GetPrimAtPath(prefix):
                prim_spec = Sdf.CreatePrimInLayer(new_layer, prefix)
                prim_spec.specifier = Sdf.SpecifierOver
                prim_spec.typeName = "Xform"

        # Copy the asset subtree from source to target
        if not Sdf.CopySpec(layer, source_path, new_layer, target_path):
            self.log.error(
                f"Failed to copy prim specs: {source_path} -> {target_path}"
            )
            return

        # Set defaultPrim to the topmost prim
        new_layer.defaultPrim = prefixes[0].name

        # Clean up non-geometry prims (rig controls, materials,
        # GeomSubsets, etc.)
        self._cleanup_non_geometry(new_layer, target_path)

        # Strip material-related opinions (material:binding rels,
        # subsetFamily:* attrs, MaterialBindingAPI from apiSchemas)
        # so the original asset's assignments pass through.
        self._strip_material_bindings(new_layer, target_path)

        # Convert all specifiers from 'def' to 'over' — this is an
        # override layer, not a definition layer.
        self._convert_to_over_specifiers(new_layer, target_path)

        # Apply resetXformStack before saving — all in-memory, no
        # layer-cache or mmap issues.
        if reset_xform_stack:
            self._apply_reset_xform_stack_sdf(new_layer, target_path)

        # Save the remapped layer (single write to disk)
        new_layer.Export(filepath)
        self.log.info(
            f"Hierarchy remapped successfully: {source_path} -> {target_path}"
        )

    def _apply_reset_xform_stack_sdf(self, layer, root_path):
        """Add ``!resetXformStack!`` via the Sdf (scene-description) API.

        Directly manipulates the ``xformOpOrder`` attribute on PrimSpecs
        in the given layer.  This avoids having to open a ``Usd.Stage``
        (and the layer-cache / mmap problems that come with it on
        Windows when the file was just rewritten by ``Export()``).

        The reset token is prepended to the existing ``xformOpOrder``
        list.  If no ``xformOpOrder`` exists yet, one is created with
        just the reset token.

        Applied to:
        - The prim at *root_path* itself (the asset root)
        - All direct children whose typeName is Mesh, Xform, or Scope
        """
        RESET_TOKEN = "!resetXformStack!"
        xformable_types = {"Xform", "Scope", "Mesh"}

        def _set_reset(prim_spec):
            """Prepend !resetXformStack! to xformOpOrder on a PrimSpec."""
            if prim_spec is None:
                return False

            attr = prim_spec.attributes.get("xformOpOrder")
            if attr is not None:
                current = list(attr.default)
                if RESET_TOKEN in current:
                    return False  # already present
                attr.default = [RESET_TOKEN] + current
            else:
                # Create the attribute with just the reset token
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

        # Apply to the root prim
        root_spec = layer.GetPrimAtPath(root_path)
        if root_spec and root_spec.typeName in xformable_types:
            modified |= _set_reset(root_spec)

        # Apply to direct children
        if root_spec:
            for child_spec in root_spec.nameChildren:
                if child_spec.typeName in xformable_types:
                    modified |= _set_reset(child_spec)

        if modified:
            self.log.info(
                "Applied !resetXformStack! to prevent double-transforms"
            )

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

    def _find_prim_by_name(self, layer, name):
        """Find first prim with exact name match via depth-first search."""

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
        """Find prim whose name ends with ':name' (namespace handling).

        When stripNamespaces is False, prim names may include namespaces
        like 'myNs:cone_character'. This matches those cases.
        """
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

    # ------------------------------------------------------------------
    # Override-layer sanitisation
    # ------------------------------------------------------------------

    def _strip_material_bindings(self, layer, root_path):
        """Remove all material-related opinions from the cache layer.

        The Maya USD exporter authors ``material:binding`` relationships,
        ``MaterialBindingAPI`` in ``apiSchemas``, and ``subsetFamily:*``
        attributes on Mesh prims.  After GeomSubset and Material prims
        are removed by ``_cleanup_non_geometry``, these become broken
        opinions that — because sublayer > reference in LIVRPS —
        override the *valid* bindings from the original asset.

        Stripping them lets the original asset's material assignments
        pass through the composition unmodified.
        """
        stripped = 0

        def _strip(path):
            nonlocal stripped
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return

            # 1. Remove material:binding* relationships
            rels_to_remove = [
                rel.name for rel in spec.relationships
                if rel.name.startswith("material:binding")
            ]
            for name in rels_to_remove:
                spec.RemoveProperty(spec.relationships[name])
                stripped += 1

            # 2. Remove subsetFamily:* attributes (orphaned after
            #    GeomSubset removal, e.g. subsetFamily:materialBind:*)
            attrs_to_remove = [
                attr.name for attr in spec.attributes
                if attr.name.startswith("subsetFamily:")
            ]
            for name in attrs_to_remove:
                spec.RemoveProperty(spec.attributes[name])
                stripped += 1

            # 3. Remove MaterialBindingAPI from apiSchemas
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
        """Change ``def`` specifiers to ``over`` on all prims.

        A point-cache sublayer is an *override* layer: it only needs to
        author the properties that differ from the original asset (e.g.
        animated ``points``).  Using ``over`` instead of ``def`` means
        that any property **not** authored here (materials, display
        color, visibility, etc.) transparently passes through from the
        weaker reference layer that holds the original asset.
        """
        # Convert the parent hierarchy prims (above the asset root)
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
        self.log.debug("Converted all prim specifiers to 'over'")

    # ------------------------------------------------------------------
    # Non-geometry cleanup
    # ------------------------------------------------------------------

    def _cleanup_non_geometry(self, layer, root_path):
        """Remove non-geometry prims from the pointcache.

        For a pointcache sublayer we only need Mesh geometry and its
        parent hierarchy (Xform, Scope). This removes:
        - BasisCurves (rig control shapes)
        - Material / Shader / NodeGraph prims
        - MayaReference prims
        - GeomSubset prims (face-material assignments come from the
          original asset; the cache copies carry namespace-mangled names)
        - Empty Xform/Scope containers with no geometry descendants
        """
        non_geo_types = {
            "BasisCurves", "Material", "Shader",
            "NodeGraph", "MayaReference", "GeomSubset",
        }

        # Pass 1: collect non-geometry typed prims
        prims_to_remove = []

        def _collect_non_geo(path):
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return
            if spec.typeName in non_geo_types:
                prims_to_remove.append(path)
                return  # skip children - they'll be removed with parent
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

        # Pass 2: remove empty Xform/Scope containers
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
