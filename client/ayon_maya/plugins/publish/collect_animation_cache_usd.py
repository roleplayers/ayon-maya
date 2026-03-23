"""Collect animation cache USD instance data.

This collector enriches the animation cache USD instance with:
- Animation frame range and sampling settings
- Original asset prim path(s) (auto-detected from containers)
- Department/layer information from task context
- Animated members from the instance

Multi-asset support:
When the instance's ``setMembers`` span multiple Maya namespaces (i.e.
multiple loaded USD assets), the collector detects a prim path for
**each** namespace and stores them in ``allAssetPrimPaths``.  The
extractor uses this dict to remap every asset in a single pass.
"""

import maya.cmds as cmds
import pyblish.api
from ayon_core.pipeline.constants import AVALON_CONTAINER_ID
from ayon_maya.api import plugin


class CollectAnimationCacheUsd(plugin.MayaInstancePlugin):
    """Collect animation cache USD instance data.

    Prepares instance data for animation cache USD publishing by:
    1. Validating animated members exist
    2. Detecting original asset prim path(s) from loaded containers
    3. Setting up animation frame range and sampling
    4. Determining department from task context
    """

    order = pyblish.api.CollectorOrder + 0.5
    families = ["animationCacheUsd"]
    label = "Collect Animation Cache USD"

    def process(self, instance):
        """Collect and prepare animation cache USD instance data."""

        # 1. Validate animated members exist
        set_members = instance.data.get("setMembers", [])
        if not set_members:
            self.log.warning(
                f"Instance {instance.name} has no members selected. "
                "Please select the animated nodes."
            )

        # 2. Detect original asset prim path(s) via smart detection
        #    Builds a dict {namespace: prim_path} for all member assets.
        all_paths, containers = self._detect_all_asset_prim_paths(
            instance
        )
        instance.data["allAssetPrimPaths"] = all_paths

        # Keep single-path key for backward compat / logging
        if all_paths:
            first_path = next(iter(all_paths.values()))
            instance.data["originalAssetPrimPath"] = first_path
        else:
            # Try single-asset fallback (manual input or UFE)
            single = self._detect_asset_prim_path_fallback(instance)
            instance.data["originalAssetPrimPath"] = single
            if single:
                all_paths["_single"] = single
                instance.data["allAssetPrimPaths"] = all_paths

        if not all_paths:
            self.log.warning(
                f"Could not auto-detect asset prim path for "
                f"{instance.name}. The contribution layer may not be "
                f"placed correctly. Set 'Original Asset Prim Path' "
                f"manually if auto-detection fails."
            )

        # 3. Detect department from task context
        department = self._detect_department(instance)
        instance.data["departmentLayer"] = department

        # 4. Prepare animation sampling settings
        creator_attrs = instance.data.get("creator_attributes", {})
        sampling_mode = creator_attrs.get("animationSampling", "sparse")
        custom_step = creator_attrs.get("customStepSize", 1.0)

        instance.data["samplingMode"] = sampling_mode
        instance.data["customStepSize"] = custom_step

        # 5. Log collected information
        self.log.info(
            f"Collected animation cache USD: "
            f"assets={list(all_paths.keys())}, "
            f"department={department}, "
            f"sampling={sampling_mode}, "
            f"members={len(set_members)}"
        )

    # ------------------------------------------------------------------
    # Multi-asset prim path detection
    # ------------------------------------------------------------------

    def _detect_all_asset_prim_paths(self, instance):
        """Detect prim paths for ALL assets represented in the instance.

        Groups members by Maya namespace, then matches each namespace
        against USD containers in the scene.

        Returns:
            tuple: (dict {namespace: prim_path}, list of containers)
        """
        set_members = instance.data.get("setMembers", [])
        member_namespaces = self._extract_namespaces(set_members)

        containers = self._get_all_containers()
        if not containers:
            return {}, []

        # Single container → use it regardless of namespace
        if len(containers) == 1:
            ns = next(iter(member_namespaces)) if member_namespaces else ""
            return {ns: containers[0]["prim_path"]}, containers

        # Multiple containers: match each member namespace
        matched = {}
        if member_namespaces:
            for container in containers:
                c_ns = container["namespace"]
                c_name = container["name"]
                for ns in member_namespaces:
                    if ns in matched:
                        continue  # already matched
                    if (c_ns and c_ns == ns) or (c_name and c_name == ns):
                        matched[ns] = container["prim_path"]
                        self.log.debug(
                            f"Matched namespace '{ns}' -> "
                            f"{container['prim_path']}"
                        )

        # If no namespace matches found, fall back to first container
        if not matched and containers:
            ns = next(iter(member_namespaces)) if member_namespaces else ""
            matched[ns] = containers[0]["prim_path"]
            self.log.debug(
                f"No namespace match, using first container: "
                f"{containers[0]['prim_path']}"
            )

        return matched, containers

    def _detect_asset_prim_path_fallback(self, instance):
        """Fallback detection: manual input or UFE selection.

        Used when container-based detection finds nothing.

        Returns:
            str: Detected prim path or empty string.
        """
        creator_attrs = instance.data.get("creator_attributes", {})

        # Manual input (user override)
        manual_input = creator_attrs.get(
            "originalAssetPrimPath", ""
        ).strip()
        if manual_input:
            self.log.debug(f"Using manual asset prim path: {manual_input}")
            return manual_input

        # UFE selection
        prim_path = self._detect_from_ufe_selection()
        if prim_path:
            self.log.info(
                f"Auto-detected asset prim path from UFE: {prim_path}"
            )
            return prim_path

        return ""

    # ------------------------------------------------------------------
    # Container discovery
    # ------------------------------------------------------------------

    def _get_all_containers(self):
        """Find all AYON containers in USD proxy shapes.

        Returns:
            list[dict]: Each dict has 'prim_path', 'namespace', 'name'.
        """
        try:
            import mayaUsd
        except ImportError:
            self.log.debug(
                "mayaUsd module not available, cannot detect containers"
            )
            return []

        proxy_shapes = cmds.ls(type="mayaUsdProxyShape", long=True) or []
        all_containers = []

        for proxy_shape in proxy_shapes:
            try:
                stage = mayaUsd.ufe.getStage(proxy_shape)
                if not stage:
                    continue

                for prim in stage.Traverse():
                    container_id = prim.GetCustomDataByKey("ayon:id")
                    if container_id == AVALON_CONTAINER_ID:
                        all_containers.append({
                            "prim_path": str(prim.GetPath()),
                            "namespace": (
                                prim.GetCustomDataByKey(
                                    "ayon:namespace"
                                ) or ""
                            ),
                            "name": (
                                prim.GetCustomDataByKey("ayon:name") or ""
                            ),
                        })
            except (RuntimeError, AttributeError) as e:
                self.log.debug(
                    f"Could not get stage from {proxy_shape}: {e}"
                )
                continue

        self.log.debug(
            f"Found {len(all_containers)} USD container(s) in scene"
        )
        return all_containers

    def _extract_namespaces(self, members):
        """Extract unique Maya namespaces from member node names.

        When 'Edit as Maya Data' loads a .mb, nodes are created under
        a namespace (e.g., 'myNamespace:pCube1'). We extract these to
        match against container metadata.

        Args:
            members: List of Maya DAG node paths

        Returns:
            set: Unique namespaces found in member names
        """
        namespaces = set()
        for member in members:
            # Get the short name (last component of long path)
            short_name = member.rsplit("|", 1)[-1]
            if ":" in short_name:
                ns = short_name.rsplit(":", 1)[0]
                # Handle nested namespaces - get the root namespace
                root_ns = ns.split(":")[0]
                namespaces.add(root_ns)
                namespaces.add(ns)
        return namespaces

    def _detect_from_ufe_selection(self):
        """Detect asset prim path from UFE USD prim selection.

        Returns:
            str: Detected prim path or empty string
        """
        try:
            from ayon_maya.api import usdlib

            for ufe_path in usdlib.iter_ufe_usd_selection():
                if "," in ufe_path:
                    _node, prim_path = ufe_path.split(",", 1)
                    if prim_path:
                        return prim_path

        except Exception as e:
            self.log.debug(f"Error detecting from UFE selection: {e}")

        return ""

    # ------------------------------------------------------------------
    # Department auto-detection
    # ------------------------------------------------------------------

    def _detect_department(self, instance):
        """Auto-detect department from AYON task context.

        Checks both task name and task type against known department
        mappings for maximum compatibility across studio naming
        conventions.

        Returns:
            str: Department name (animation, layout, cfx, fx) or "auto"
                 if detection fails.
        """

        creator_attrs = instance.data.get("creator_attributes", {})
        department = creator_attrs.get("department", "auto")

        if department != "auto":
            return department

        # Auto-detect from task context
        try:
            task_entity = instance.data.get("taskEntity")
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
                        self.log.debug(
                            f"Auto-detected department: {dept} "
                            f"from task: {task_name} (type: {task_type})"
                        )
                        return dept

        except Exception as e:
            self.log.debug(f"Error auto-detecting department: {e}")

        return "auto"
