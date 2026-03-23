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


def _get_pulled_prims():
    """Find all pulled MayaReference prims and their DAG paths.

    When "Edit as Maya Data" is active, Maya USD stores
    ``Maya:Pull:DagPath`` on the prim.  The **parent** prim name
    is the asset name.

    Returns:
        list[tuple[str, str]]: ``[(dag_path, asset_name), ...]``.
    """
    try:
        import mayaUsd.ufe
    except ImportError:
        return []

    pulled = []
    proxy_shapes = cmds.ls(type="mayaUsdProxyShape", long=True) or []

    for proxy in proxy_shapes:
        try:
            stage = mayaUsd.ufe.getStage(proxy)
            if not stage:
                continue

            for prim in stage.TraverseAll():
                dag = prim.GetCustomDataByKey("Maya:Pull:DagPath")
                if not dag:
                    continue

                parent = prim.GetParent()
                if not parent or parent.IsPseudoRoot():
                    continue

                asset_name = parent.GetName()
                pulled.append((dag, asset_name))
                print(
                    f"[AnimCacheUSD] Pulled: {prim.GetPath()} "
                    f"dag='{dag}' -> asset='{asset_name}'"
                )

        except Exception as exc:
            print(f"[AnimCacheUSD] Error on {proxy}: {exc}")
            continue

    return pulled


def _match_namespaces_to_assets(ns_groups, pulled_prims):
    """Match namespace groups to asset names via DAG path prefix.

    Each pulled prim has a DAG path (e.g.
    ``|__mayaUsd__|rigParent|rig``).  Members of a namespace group
    are children of that DAG hierarchy (e.g.
    ``|__mayaUsd__|rigParent|rig|rigMain:gibro|...``).

    By checking which pulled prim's DAG path is a prefix of the
    members in each group, we can map the namespace to the asset.

    Args:
        ns_groups: ``{namespace: [member_paths]}``.
        pulled_prims: ``[(dag_path, asset_name), ...]``.

    Returns:
        dict[str, str]: ``{maya_namespace: asset_prim_name}``.
    """
    ns_to_asset = {}

    for ns, members in ns_groups.items():
        for dag_path, asset_name in pulled_prims:
            prefix = dag_path + "|"
            if any(m.startswith(prefix) for m in members):
                ns_to_asset[ns] = asset_name
                print(
                    f"[AnimCacheUSD] MATCHED: ns='{ns}' "
                    f"-> asset='{asset_name}' "
                    f"(via dag prefix '{dag_path}')"
                )
                break

    return ns_to_asset


def _group_members_by_namespace(members):
    """Group Maya DAG nodes by their root namespace.

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
        its product name uses the **asset name** (from the pulled USD
        prim's parent), not the Maya namespace.

        Selected parent transforms are automatically expanded to their
        descendant mesh geometry so the user can select rig roots or
        asset groups without worrying about picking exact mesh nodes.
        """

        members = cmds.ls(selection=True, long=True, type="dagNode")
        print(f"[AnimCacheUSD] Selection: {members}")

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
        print(
            f"[AnimCacheUSD] After geometry expansion: "
            f"{len(members)} nodes"
        )

        split_per_asset = pre_create_data.get("splitPerAsset", True)

        if not split_per_asset:
            cmds.select(members, replace=True, noExpand=True)
            return super().create(
                product_name, instance_data, pre_create_data
            )

        # --- Multi-asset split ---
        groups = _group_members_by_namespace(members)
        print(
            f"[AnimCacheUSD] Namespace groups: "
            f"{list(groups.keys())}"
        )

        # Single group → no split needed
        if len(groups) <= 1:
            cmds.select(members, replace=True, noExpand=True)
            return super().create(
                product_name, instance_data, pre_create_data
            )

        # Resolve Maya namespace → USD asset name by matching
        # each group's member DAG paths against pulled prim roots.
        pulled_prims = _get_pulled_prims()
        ns_to_asset = _match_namespaces_to_assets(groups, pulled_prims)
        print(f"[AnimCacheUSD] ns_to_asset = {ns_to_asset}")

        self.log.info(
            f"Splitting selection into {len(groups)} asset instances: "
            f"{list(groups.keys())}"
        )

        project_name = self.create_context.get_current_project_name()
        folder_entity = self.create_context.get_current_folder_entity()
        task_entity = self.create_context.get_current_task_entity()

        instances = []
        for namespace, ns_members in sorted(groups.items()):
            # Look up asset name.  Try exact match, then prefix.
            asset_name = ns_to_asset.get(namespace)
            if not asset_name:
                for stored_ns, name in ns_to_asset.items():
                    if namespace.startswith(stored_ns):
                        asset_name = name
                        break
            if not asset_name:
                asset_name = namespace or "default"

            print(
                f"[AnimCacheUSD] namespace='{namespace}' "
                f"-> asset='{asset_name}'"
            )

            # Build variant: "Main" + "Gibro" → "MainGibro"
            clean = asset_name.replace(":", "_").replace(" ", "_")
            suffix = "".join(
                p.capitalize() for p in clean.split("_") if p
            )
            base_variant = instance_data.get(
                "variant", self.get_default_variant()
            )
            variant = base_variant + suffix

            asset_product_name = self.get_product_name(
                project_name, folder_entity, task_entity, variant,
            )

            asset_data = dict(instance_data)
            asset_data["variant"] = variant

            self.log.info(
                f"Creating '{asset_product_name}' — "
                f"namespace='{namespace}', asset='{asset_name}', "
                f"{len(ns_members)} members"
            )

            # Select this namespace's members before super().create()
            # reads cmds.ls(selection=True)
            cmds.select(ns_members, replace=True, noExpand=True)

            inst = super().create(
                asset_product_name, asset_data, pre_create_data
            )
            instances.append(inst)

        # Restore original selection
        cmds.select(members, replace=True, noExpand=True)

        return instances[-1] if instances else None
