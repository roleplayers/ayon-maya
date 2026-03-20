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
3. Result: Clean point cache with mesh animation

Usage:
- Select: /assets/character/cone_character/geo/cone_character_GEO (or similar)
- Publish with animationCacheUsd family
- Get: point_cache.usd with animated mesh points
"""

import os

from ayon_core.pipeline import PublishValidationError
from ayon_maya.api import plugin
from ayon_maya.api.lib import maintained_selection
from maya import cmds
from pxr import Sdf, Usd


def parse_version(version_str):
    """Parse string like '0.21.0' to (0, 21, 0)"""
    return tuple(int(v) for v in version_str.split("."))


def find_mesh_shapes(nodes):
    """Find all mesh shapes under given nodes (recursively).

    Args:
        nodes: List of node names/paths

    Returns:
        List of mesh shape nodes
    """
    shapes = []
    for node in nodes:
        # Get all shapes under this node (including nested)
        try:
            # Use ls to find all mesh shapes in subtree
            found_shapes = cmds.ls(f"{node}|*", type="mesh", long=True)
            if not found_shapes:
                # Try direct children if it's a transform
                found_shapes = cmds.listRelatives(node, shapes=True, type="mesh", allDescendents=True)
            if found_shapes:
                shapes.extend(found_shapes)
            else:
                # If node itself is a mesh, use it
                if cmds.nodeType(node) == "mesh":
                    shapes.append(node)
        except Exception as e:
            pass

    return list(set(shapes)) if shapes else []


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
        2. Clean up any unwanted structure
        3. Generate representation
        """

        staging_dir = self.staging_dir(instance)

        # 1. Export animation cache USD (point cache)
        self.log.info("Exporting animation cache USD (point cache)...")
        cache_file = self._export_animation_cache(instance, staging_dir)
        cache_filename = os.path.basename(cache_file)

        # 2. Add representation
        if "representations" not in instance.data:
            instance.data["representations"] = []

        # Main representation: Animation cache USD
        instance.data["representations"].append({
            "name": "usd",
            "ext": "usd",
            "files": cache_filename,
            "stagingDir": staging_dir
        })

        self.log.info(f"✓ Extracted point cache: {cache_filename}")

    def _find_geometry_shapes(self, nodes):
        """Find mesh shapes in the selected nodes or their siblings/parents.

        Smart search strategy:
        1. If it's a shape, use it directly
        2. Find shapes inside the node (descendants)
        3. Find shapes in sibling 'geo' groups
        4. Search entire parent structure for geo nodes
        """
        shapes = []
        searched_paths = set()

        for node in nodes:
            # 1. Check if it's already a shape
            try:
                node_type = cmds.nodeType(node)
                if node_type == "mesh":
                    shapes.append(node)
                    continue
            except:
                pass

            # 2. Find shapes as descendants inside this node
            try:
                descendants = cmds.listRelatives(
                    node, shapes=True, type="mesh", allDescendents=True, fullPath=True
                )
                if descendants:
                    shapes.extend(descendants)
                    searched_paths.add(node)
                    continue
            except:
                pass

            # 3. If parent has a 'geo' sibling, search there
            try:
                parent = cmds.listRelatives(node, parent=True, fullPath=True)
                if parent:
                    parent = parent[0]
                    # Look for geo group in parent's children
                    children = cmds.listRelatives(parent, children=True, fullPath=True) or []
                    for child in children:
                        if "geo" in child.lower() and child not in searched_paths:
                            geo_shapes = cmds.listRelatives(
                                child, shapes=True, type="mesh", allDescendents=True, fullPath=True
                            )
                            if geo_shapes:
                                shapes.extend(geo_shapes)
                                searched_paths.add(child)
                                break
            except:
                pass

            # 4. Search entire asset root for any geo/mesh
            if not shapes:
                try:
                    # Get root - trace back to topmost parent
                    current = node
                    root = node
                    while True:
                        parent = cmds.listRelatives(current, parent=True, fullPath=True)
                        if not parent:
                            root = current
                            break
                        root = parent[0]
                        current = root

                    # Now search from root for geo nodes
                    all_children = cmds.listRelatives(root, allDescendents=True, fullPath=True) or []
                    geo_nodes = [n for n in all_children if "geo" in n.lower()]

                    for geo_node in geo_nodes:
                        if geo_node not in searched_paths:
                            geo_shapes = cmds.listRelatives(
                                geo_node, shapes=True, type="mesh", allDescendents=True, fullPath=True
                            )
                            if geo_shapes:
                                shapes.extend(geo_shapes)
                                searched_paths.add(geo_node)
                                break
                except Exception as e:
                    self.log.debug(f"Could not search from root: {e}")

            # 5. Last resort: direct wildcard search in node's subtree
            if not shapes:
                try:
                    found = cmds.ls(f"{node}|*", type="mesh", long=True)
                    if found:
                        shapes.extend(found)
                except:
                    pass

        return list(set(shapes)) if shapes else []

    def _filter_deformed_shapes(self, shapes):
        """Filter out non-deformed shapes (e.g., ShapeOrig).

        Keep only shapes that are actually deformed.
        Removes:
        - ShapeOrig (original non-deformed shape)
        - Duplicate shapes
        """
        if not shapes:
            return []

        filtered = []
        seen_base_names = set()

        for shape in shapes:
            # Skip "Orig" shapes - these are non-deformed originals
            if shape.endswith("Orig") or "Orig" in shape:
                self.log.debug(f"Skipping non-deformed shape: {shape}")
                continue

            # Get base name (without Shape suffix)
            base_name = shape.replace("Shape", "").replace("Orig", "")

            # Skip if we've already added this mesh (different variant)
            if base_name in seen_base_names:
                self.log.debug(f"Skipping duplicate shape: {shape}")
                continue

            filtered.append(shape)
            seen_base_names.add(base_name)

        return filtered

    def _suggest_geometry_path(self, nodes):
        """Show what was searched and suggest correct selection."""
        msg_lines = ["POINT CACHE requires: mesh/nurbsSurface shapes with animation"]
        msg_lines.append("WRONG: Selecting rig groups or control transforms")
        msg_lines.append("RIGHT: Select the deformed geometry (geo/mesh_SHAPE)\n")

        for node in nodes:
            msg_lines.append(f"Selected: {node}")

            # Try to show the actual hierarchy for debugging
            try:
                # Get root and show children
                current = node
                root = node
                while True:
                    parent = cmds.listRelatives(current, parent=True, fullPath=True)
                    if not parent:
                        root = current
                        break
                    root = parent[0]
                    current = root

                msg_lines.append(f"Asset root: {root}")

                # List immediate children and geo nodes
                all_desc = cmds.listRelatives(root, allDescendents=True, fullPath=True) or []
                geo_nodes = [n for n in all_desc if "geo" in n.lower()]

                if geo_nodes:
                    msg_lines.append("Found GEO groups:")
                    for geo in geo_nodes[:3]:  # Show first 3
                        msg_lines.append(f"  → {geo}")
                        # Show shapes inside
                        shapes = cmds.listRelatives(
                            geo, shapes=True, type="mesh", allDescendents=True, fullPath=True
                        )
                        if shapes:
                            for shape in shapes[:2]:
                                msg_lines.append(f"      └─ {shape}")
                else:
                    msg_lines.append("No GEO groups found in hierarchy")

            except Exception as e:
                msg_lines.append(f"Could not analyze hierarchy: {e}")

        return "\n".join(msg_lines)

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

        # IMPORTANT: Validate and find actual geometry
        # If members are transforms/groups, try to find shapes inside them
        shapes_to_export = self._find_geometry_shapes(members)

        # Filter out non-deformed shapes (e.g., ShapeOrig)
        # Keep only shapes that are actually deformed
        shapes_to_export = self._filter_deformed_shapes(shapes_to_export)

        if not shapes_to_export:
            raise PublishValidationError(
                f"No geometry found to export!\n"
                f"Selected nodes: {members}\n\n"
                f"POINT CACHE requires: mesh/nurbsSurface shapes with animation\n"
                f"WRONG: Selecting rig groups or control transforms\n"
                f"RIGHT: Select the deformed geometry (geo/mesh_SHAPE)\n\n"
                f"Try selecting: {self._suggest_geometry_path(members)}"
            )

        self.log.info(f"Exporting point cache for: {shapes_to_export}")
        self.log.debug(f"Frame range: {instance.data.get('frameStart', 1)}-{instance.data.get('frameEnd', 1)}")
        self.log.debug(f"Sampling: {sampling_mode} (step: {frame_step})")

        # Prepare export options for POINT CACHE
        # Key: exportSkels and exportSkin should be "none" to skip rig structure
        options = {
            "file": filepath,
            "frameRange": (
                instance.data.get("frameStart", 1),
                instance.data.get("frameEnd", 1)
            ),
            "frameStride": frame_step,
            # CRITICAL: Skip rig/skeleton export - we only want geometry
            "exportSkels": "none",  # Don't export skeleton
            "exportSkin": "none",  # Don't export skin clusters
            "exportBlendShapes": False,  # Don't export blend shapes (causes conflicts)
            # Other settings
            # IMPORTANT: Keep namespaces - shot hierarchy needs them!
            "stripNamespaces": False,  # was: creator_attrs.get("stripNamespaces", True)
            "mergeTransformAndShape": False,  # Keep transform and shape separate
            "exportDisplayColor": False,
            "exportVisibility": False,
            "exportColorSets": False,
            "exportUVs": True,  # Keep UVs for texture mapping
            "exportInstances": False,
            "defaultUSDFormat": "usdc",  # Compressed binary
            "staticSingleSample": False,  # Keep animation keyframes
            "eulerFilter": True,
        }

        # Try to use worldspace if available (Maya USD 0.21.0+)
        try:
            maya_usd_version = parse_version(
                cmds.pluginInfo("mayaUsdPlugin", query=True, version=True)
            )
            if maya_usd_version >= (0, 21, 0):
                options["worldspace"] = True
            else:
                self.log.debug(f"Maya USD {maya_usd_version} < 0.21.0, no worldspace")
        except Exception as e:
            self.log.debug(f"Could not determine Maya USD version: {e}")

        self.log.debug(f"Export options: {options}")
        self.log.debug(f"Exporting shapes: {shapes_to_export}")

        # Export USD with animation
        with maintained_selection():
            cmds.select(shapes_to_export, replace=True, noExpand=True)
            try:
                cmds.mayaUSDExport(**options)
            except RuntimeError as e:
                # Try to get the actual Maya error message
                error_msg = str(e)
                try:
                    # Check Maya's command output for details
                    mel_output = cmds.commandEcho(query=True)
                    if mel_output:
                        error_msg += f"\nMaya output: {mel_output}"
                except:
                    pass

                self.log.error(f"Export failed with error: {error_msg}")
                raise PublishValidationError(
                    f"Failed to export USD point cache: {error_msg}\n"
                    f"Exporting: {shapes_to_export}"
                )

        if not os.path.exists(filepath):
            raise PublishValidationError(
                f"USD export failed, file not created: {filepath}"
            )

        self.log.debug(f"Exported point cache USD: {filepath}")
        return filepath
