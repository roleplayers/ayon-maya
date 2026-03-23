"""Create Animation Cache USD instance.

This creator enables publishing of animated USD assets that were edited
as Maya data. The animation cache is exported as USD and can be used as
a contribution layer in the shot composition.

Workflow:
1. Load USD asset in shot with "Edit as Maya Data"
2. Animate the geometry
3. Create animationCacheUsd instance
4. Publish to generate:
   - animation_cache.usd: Sparse animation data with animated points

Multi-asset support:
When "Split per Asset" is enabled and multiple assets are selected, the
creator automatically groups members by their Maya namespace (each
loaded USD asset gets its own namespace) and creates one publish
instance per asset.  Each instance is named with the asset identifier
(from the USD container, not the Maya namespace) so that individual
cache layers can be updated independently.

When "Split per Asset" is disabled, all selected geometry is exported
into a single cache.  The extractor and collector handle multiple
assets in a single instance by detecting all prim paths and remapping
every asset subtree into the output layer.
"""

from ayon_maya.api import plugin, lib
from ayon_core.lib import (
    BoolDef,
    EnumDef,
    NumberDef,
    TextDef
)
from maya import cmds


# Custom data key set by Maya USD when a prim is "pulled" into Maya
# via "Edit as Maya Data".  The value is the Maya DAG path of the
# pulled node (e.g. ``"|rigMain:rig"``).
_PULL_DG_KEY = "Maya:Pull:DagPath"


def _get_namespace_to_asset_name():
    """Map Maya namespaces to USD asset prim names.

    When "Edit as Maya Data" is active on a MayaReference prim,
    Maya USD stores a ``Maya:Pull:DagPath`` custom-data key on that
    prim with the Maya DAG path of the pulled node.  The **parent**
    of that prim in the USD hierarchy is the asset prim (e.g.
    ``/assets/character/gibro``).

    ``stage.TraverseAll()`` visits prims even when they are inactive
    (pulled), so we can always find the MayaReference prim and read
    both the DAG path (→ Maya namespace) and the parent prim name
    (→ asset name).

    Returns:
        dict[str, str]: ``{maya_namespace: asset_prim_name}``.
    """
    try:
        import mayaUsd.ufe
    except ImportError:
        return {}

    ns_to_name = {}
    proxy_shapes = cmds.ls(type="mayaUsdProxyShape", long=True) or []

    for proxy in proxy_shapes:
        try:
            stage = mayaUsd.ufe.getStage(proxy)
            if not stage:
                continue

            for prim in stage.TraverseAll():
                dag_path = prim.GetCustomDataByKey(_PULL_DG_KEY)
                if not dag_path:
                    continue

                # dag_path is e.g. "|rigMain:rig" or "|rigMain1:rig"
                # Extract the Maya namespace from it.
                short = dag_path.rsplit("|", 1)[-1]
                if ":" not in short:
                    continue
                maya_ns = short.split(":")[0]

                # The parent prim name IS the asset name.
                parent = prim.GetParent()
                if not parent or parent.IsPseudoRoot():
                    continue

                asset_name = parent.GetName()
                ns_to_name[maya_ns] = asset_name

        except (RuntimeError, AttributeError):
            continue

    return ns_to_name


def _group_members_by_namespace(members):
    """Group Maya DAG nodes by their root namespace.

    When an asset is loaded via "Edit as Maya Data" each asset lives
    under its own Maya namespace (e.g. ``cone_character_01:pCube1``).
    This function groups members so that each group corresponds to
    one asset.

    Members **without** a namespace are collected under the key ``""``.

    Args:
        members (list[str]): Long DAG paths.

    Returns:
        dict[str, list[str]]: ``{namespace: [members]}``.
    """
    groups = {}
    for member in members:
        short_name = member.rsplit("|", 1)[-1]
        if ":" in short_name:
            ns = short_name.split(":")[0]
        else:
            ns = ""
        groups.setdefault(ns, []).append(member)
    return groups


def _expand_to_geometry(members):
    """Expand parent transforms to include descendant mesh geometry.

    When a user selects a parent group or rig root, this finds all
    mesh shapes underneath and returns their parent transforms.  This
    ensures the export captures the actual deformed geometry even when
    the user selects a high-level node.

    If the selection already contains mesh shapes or their transforms,
    they are kept as-is.

    Args:
        members (list[str]): Long DAG paths from selection.

    Returns:
        list[str]: Expanded list including descendant mesh transforms.
    """
    # Check if any selected node already has mesh shapes
    has_mesh = False
    for member in members:
        if cmds.nodeType(member) == "mesh":
            has_mesh = True
            break
        shapes = cmds.listRelatives(
            member, shapes=True, type="mesh", fullPath=True
        ) or []
        if shapes:
            has_mesh = True
            break

    if has_mesh:
        return members

    # No meshes in selection — find all descendant meshes
    expanded = set()
    for member in members:
        meshes = cmds.listRelatives(
            member, allDescendents=True, type="mesh", fullPath=True
        ) or []
        if meshes:
            transforms = cmds.listRelatives(
                meshes, parent=True, fullPath=True
            ) or []
            expanded.update(transforms)

    if expanded:
        return list(expanded)

    # Nothing found — return original
    return members


class CreateAnimationCacheUsd(plugin.MayaCreator):
    """Create Animation Cache USD from Maya scene objects"""

    identifier = "io.ayon.creators.maya.animationcacheusd"
    label = "Animation Cache USD"
    product_base_type = "usd"
    product_type = "animationCacheUsd"
    icon = "circle-play"
    description = "Create Animation Cache USD Export"

    def get_publish_families(self):
        return ["animationCacheUsd", "usd"]

    def get_pre_create_attr_defs(self):
        defs = super().get_pre_create_attr_defs()

        defs.append(
            BoolDef("splitPerAsset",
                    label="Split per Asset",
                    default=True,
                    tooltip=(
                        "When enabled and multiple assets are selected, "
                        "a separate publish instance is created for each "
                        "asset (grouped by Maya namespace).\n\n"
                        "This produces independent cache layers per asset, "
                        "allowing CFX/FX to update individual character "
                        "caches without affecting others.\n\n"
                        "When disabled, all selected geometry is exported "
                        "into a single cache."
                    ))
        )

        return defs

    def get_attr_defs_for_instance(self, instance):
        """Get attribute definitions for this instance."""

        defs = lib.collect_animation_defs(
            create_context=self.create_context)

        # Animation sampling strategy
        defs.append(
            EnumDef("animationSampling",
                    label="Animation Sampling",
                    items={
                        "sparse": "Sparse (keyframes only)",
                        "per_frame": "Per Frame",
                        "custom": "Custom Step"
                    },
                    default="sparse",
                    tooltip=(
                        "sparse: Only animated keys (minimal file size)\n"
                        "per_frame: All frames sampled (complete data)\n"
                        "custom: Custom step size for sampling"
                    ))
        )

        defs.append(
            NumberDef(
                "customStepSize",
                label="Custom Step Size",
                default=1.0,
                decimals=3,
                tooltip=(
                    "Step size for animation sampling.\n"
                    "1.0 = every frame, 0.5 = two samples per frame"
                )
            )
        )

        # Asset prim path (fallback if auto-detection fails)
        defs.append(
            TextDef("originalAssetPrimPath",
                    label="Original Asset Prim Path",
                    default="",
                    placeholder="/assets/character/cone_character",
                    tooltip=(
                        "Full USD prim path of the original asset in the "
                        "shot stage.\n\n"
                        "AUTO-DETECTED: Normally resolved automatically "
                        "from loaded USD containers. You do NOT need to "
                        "fill this manually unless auto-detection fails.\n\n"
                        "Example: /assets/character/cone_character"
                    ))
        )

        defs.append(
            EnumDef("defaultUSDFormat",
                    label="File Format",
                    items={
                        "usdc": "Binary",
                        "usda": "ASCII"
                    },
                    default="usda",
                    tooltip="Output USD file format")
        )

        defs.append(
            BoolDef("resetXformStack",
                    label="Reset Xform Stack",
                    default=True,
                    tooltip=(
                        "Add !resetXformStack! to the exported cache prims.\n"
                        "Prevents double-transforms when the cache is "
                        "composed under an Xform that still carries the "
                        "layout transform."
                    ))
        )

        defs.append(
            BoolDef("stripNamespaces",
                    label="Strip Namespaces",
                    default=True,
                    tooltip="Remove namespaces during export")
        )

        return defs

    def create(self, product_name, instance_data, pre_create_data):
        """Create instance(s) with selected members.

        When ``splitPerAsset`` is enabled and the selection contains
        members from more than one Maya namespace, one instance is
        created per namespace (i.e. per loaded asset).  Each instance
        receives the subset of members that belong to that asset, and
        its product name uses the **asset name** (from the USD
        container metadata), not the Maya namespace.

        Selected parent transforms are automatically expanded to their
        descendant mesh geometry so the user can select rig roots or
        asset groups without worrying about picking exact mesh nodes.
        """

        members = cmds.ls(selection=True, long=True, type="dagNode")

        if not members:
            self.log.warning(
                "No nodes selected for animation cache export. "
                "Please select the animated geometry."
            )
            return super().create(
                product_name, instance_data, pre_create_data
            )

        # Expand parent transforms to include descendant geometry
        members = _expand_to_geometry(members)
        self.log.debug(
            f"Members after geometry expansion: {len(members)} nodes"
        )

        split_per_asset = pre_create_data.get("splitPerAsset", True)

        if not split_per_asset:
            cmds.select(members, replace=True, noExpand=True)
            return super().create(
                product_name, instance_data, pre_create_data
            )

        # --- Multi-asset split ---
        groups = _group_members_by_namespace(members)

        # Single group → no split needed
        if len(groups) <= 1:
            cmds.select(members, replace=True, noExpand=True)
            return super().create(
                product_name, instance_data, pre_create_data
            )

        # Query container metadata to get proper asset names
        ns_to_asset = _get_namespace_to_asset_name()
        self.log.debug(f"Namespace → asset name map: {ns_to_asset}")

        self.log.info(
            f"Splitting selection into {len(groups)} asset instances: "
            f"{list(groups.keys())}"
        )

        project_name = self.create_context.get_current_project_name()
        folder_entity = self.create_context.get_current_folder_entity()
        task_entity = self.create_context.get_current_task_entity()

        instances = []
        for namespace, ns_members in sorted(groups.items()):
            # Use asset name from container, not the Maya namespace.
            # Maya may auto-number namespaces (rigMain → rigMain1),
            # so try exact match first, then prefix match.
            asset_name = ns_to_asset.get(namespace)
            if not asset_name:
                # Prefix match: "rigMain1" starts with "rigMain"
                for stored_ns, name in ns_to_asset.items():
                    if namespace.startswith(stored_ns):
                        asset_name = name
                        break
            if not asset_name:
                asset_name = namespace or "default"

            # Clean the name for use in variant
            # "cone_character" → "ConeCharacter"
            clean_name = asset_name.replace(":", "_").replace(" ", "_")
            variant_suffix = "".join(
                part.capitalize() for part in clean_name.split("_") if part
            )

            base_variant = instance_data.get(
                "variant",
                self.get_default_variant()
            )
            variant = base_variant + variant_suffix

            asset_product_name = self.get_product_name(
                project_name,
                folder_entity,
                task_entity,
                variant,
            )

            asset_instance_data = dict(instance_data)
            asset_instance_data["variant"] = variant

            self.log.info(
                f"Creating '{asset_product_name}' — "
                f"namespace='{namespace}', asset='{asset_name}', "
                f"{len(ns_members)} members"
            )

            # Select ONLY this group's members
            cmds.select(ns_members, replace=True, noExpand=True)

            inst = super().create(
                asset_product_name, asset_instance_data, pre_create_data
            )
            instances.append(inst)

        # Restore original selection
        cmds.select(members, replace=True, noExpand=True)

        return instances[-1] if instances else None
