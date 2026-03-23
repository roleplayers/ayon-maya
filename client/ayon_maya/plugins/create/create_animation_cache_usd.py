"""Create Animation Cache USD instance.

This creator enables publishing of animated USD assets that were edited
as Maya data. The animation cache is exported as USD and can be used as
a contribution layer in the shot composition.

Workflow:
1. Load USD asset in shot with "Edit as Maya Data"
2. Animate the geometry
3. Create animationCacheUsd instance
4. Publish to generate:
   - animation_cache.usda: Sparse animation data
   - animation_contribution.usda: Override layer for shot USD composition

Multi-asset support:
When "Split per Asset" is enabled and multiple assets are selected, the
creator automatically groups members by their Maya namespace (each
loaded USD asset gets its own namespace) and creates one publish
instance per asset. Each instance is named with the asset identifier
so that individual cache layers can be updated independently — useful
when CFX/FX need to iterate on specific characters in a multi-character
shot.
"""

from ayon_maya.api import plugin, lib
from ayon_core.lib import (
    BoolDef,
    EnumDef,
    NumberDef,
    TextDef
)
from ayon_core.pipeline.create import CreatorError
from maya import cmds


def _detect_department_from_context(create_context):
    """Detect department from the current AYON task context.

    Checks both task name and task type against known department
    mappings. Returns the matching department string or ``"auto"``
    if nothing matches.

    Args:
        create_context: The AYON create context.

    Returns:
        str: Detected department or "auto".
    """
    try:
        task_entity = create_context.get_current_task_entity()
        if not task_entity:
            return "auto"

        task_name = (task_entity.get("name") or "").lower()
        task_type = (task_entity.get("taskType") or "").lower()

        dept_mapping = {
            "anim": "animation",
            "animation": "animation",
            "layout": "layout",
            "cfx": "cfx",
            "fx": "fx",
        }

        # Check task name first, then task type
        for source in (task_name, task_type):
            for key, dept in dept_mapping.items():
                if key in source:
                    return dept

    except Exception:
        pass

    return "auto"


def _group_members_by_namespace(members):
    """Group Maya DAG nodes by their root namespace.

    When an asset is loaded via "Edit as Maya Data" each asset lives
    under its own Maya namespace (e.g. ``cone_character:pCube1``).
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

        # Get animation frame range defaults
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

        # Custom step size (visible when custom selected)
        custom_step_def = NumberDef(
            "customStepSize",
            label="Custom Step Size",
            default=1.0,
            decimals=3,
            tooltip=(
                "Step size for animation sampling.\n"
                "1.0 = every frame, 0.5 = two samples per frame"
            )
        )
        defs.append(custom_step_def)

        # Department/layer selection — default to auto-detected value
        detected_dept = _detect_department_from_context(self.create_context)
        defs.append(
            EnumDef("department",
                    label="Department",
                    items={
                        "auto": "Auto-detect from task",
                        "animation": "Animation",
                        "layout": "Layout",
                        "cfx": "CFX",
                        "fx": "FX"
                    },
                    default=detected_dept,
                    tooltip=(
                        "Department layer for the USD contribution.\n"
                        "The default is auto-detected from the current "
                        "AYON task context so that animation exports to "
                        "the animation layer, layout to layout, etc."
                    ))
        )

        # Asset prim path input (fallback if auto-detection fails)
        defs.append(
            TextDef("originalAssetPrimPath",
                    label="Original Asset Prim Path",
                    default="",
                    placeholder="/assets/character/cone_character",
                    tooltip=(
                        "Full USD prim path of the original asset in the "
                        "shot stage.\n\n"
                        "AUTO-DETECTED: This is normally resolved "
                        "automatically from loaded USD containers (the prims "
                        "with Ayon metadata). You do NOT need to fill this "
                        "manually unless auto-detection fails.\n\n"
                        "If auto-detection fails, enter the full prim path "
                        "as it appears in the USD stage outliner.\n"
                        "Example: /assets/character/cone_character"
                    ))
        )

        # USD format
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

        # Reset Xform Stack (prevent double-transforms from layout)
        defs.append(
            BoolDef("resetXformStack",
                    label="Reset Xform Stack",
                    default=True,
                    tooltip=(
                        "Add !resetXformStack! to the exported cache prims.\n"
                        "This prevents double-transforms when the cache is "
                        "composed as a sublayer under an Xform that still "
                        "carries the layout transform.\n\n"
                        "When enabled (and worldspace export is used), the "
                        "cache points are already in worldspace and ancestor "
                        "transforms will be ignored during composition."
                    ))
        )

        # Strip namespaces
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
        its product name is suffixed with the asset identifier.
        """

        members = cmds.ls(selection=True, long=True, type="dagNode")

        if not members:
            self.log.warning(
                "No nodes selected for animation cache export. "
                "Please select the animated geometry."
            )
            # Fall through to create an empty instance (user can add
            # members later via the publisher UI).
            return super().create(
                product_name, instance_data, pre_create_data
            )

        split_per_asset = pre_create_data.get("splitPerAsset", True)

        if not split_per_asset:
            # Single instance with all members
            return super().create(
                product_name, instance_data, pre_create_data
            )

        # --- Multi-asset split ---
        groups = _group_members_by_namespace(members)

        # If there's only one group, no split needed
        if len(groups) == 1:
            return super().create(
                product_name, instance_data, pre_create_data
            )

        # Multiple groups: create one instance per asset
        self.log.info(
            f"Splitting selection into {len(groups)} asset instances: "
            f"{list(groups.keys())}"
        )

        # Detect department once for all instances
        detected_dept = _detect_department_from_context(self.create_context)

        project_name = self.create_context.get_current_project_name()
        folder_entity = self.create_context.get_current_folder_entity()
        task_entity = self.create_context.get_current_task_entity()

        instances = []
        for namespace, ns_members in sorted(groups.items()):
            # Derive a clean asset label from the namespace
            asset_label = namespace if namespace else "default"
            # Clean the label for use in product names (remove special chars)
            asset_variant = asset_label.replace(":", "_").replace(" ", "_")

            # Build variant: original variant + asset name
            # e.g. "Main" + "cone_character" → "MainConeCharacter"
            base_variant = instance_data.get(
                "variant",
                self.get_default_variant()
            )
            variant = base_variant + asset_variant.title().replace("_", "")

            # Generate proper product name via AYON's naming system
            asset_product_name = self.get_product_name(
                project_name,
                folder_entity,
                task_entity,
                variant,
            )

            # Build per-instance data
            asset_instance_data = dict(instance_data)
            asset_instance_data["variant"] = variant

            self.log.info(
                f"Creating instance '{asset_product_name}' with "
                f"{len(ns_members)} members from namespace '{namespace}'"
            )

            # Select only this group's members and create
            cmds.select(ns_members, replace=True, noExpand=True)

            inst = super().create(
                asset_product_name, asset_instance_data, pre_create_data
            )
            instances.append(inst)

        # Restore original selection
        cmds.select(members, replace=True, noExpand=True)

        # Return the last created instance (AYON expects a single return)
        return instances[-1] if instances else None
