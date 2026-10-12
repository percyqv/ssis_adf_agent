"""
ADF Deployer — deploys generated ADF JSON artifacts to an existing Azure Data Factory
using the azure-mgmt-datafactory SDK.

Authentication uses DefaultAzureCredential (supports:
  - Local: az login
  - CI/CD: AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET env vars
  - Managed Identity: on Azure-hosted compute)

Supported artifact types: pipeline, linkedService, dataset, dataFlow, trigger.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.datafactory import DataFactoryManagementClient
    from azure.mgmt.datafactory.models import (
        DatasetResource,
        DataFlowResource,
        LinkedServiceResource,
        PipelineResource,
        TriggerResource,
    )
    _AZURE_AVAILABLE = True
except ImportError:
    _AZURE_AVAILABLE = False


@dataclass
class DeployResult:
    artifact_type: str
    name: str
    success: bool
    error: str | None = None


class AdfDeployer:
    """
    Deploys ADF artifacts (pipelines, linked services, datasets, data flows, triggers)
    from a directory of JSON files to an Azure Data Factory.

    Usage::

        deployer = AdfDeployer(
            subscription_id="...",
            resource_group="rg-myproject",
            factory_name="adf-myproject",
        )
        results = deployer.deploy_all(Path("output/MyPackage"))
    """

    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        factory_name: str,
        credential: Any = None,
    ) -> None:
        if not _AZURE_AVAILABLE:
            raise ImportError(
                "azure-mgmt-datafactory and azure-identity are required for AdfDeployer. "
                "Install with: pip install azure-mgmt-datafactory azure-identity"
            )
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.factory_name = factory_name
        self._credential = credential or DefaultAzureCredential()
        self._client: DataFactoryManagementClient | None = None

    @property
    def client(self) -> "DataFactoryManagementClient":
        if self._client is None:
            self._client = DataFactoryManagementClient(
                self._credential, self.subscription_id
            )
        return self._client

    def deploy_all(self, artifacts_dir: Path, dry_run: bool = False) -> list[DeployResult]:
        """
        Discover and deploy all ADF JSON artifacts in *artifacts_dir*.

        Deployment order: linkedServices → datasets → dataFlows → pipelines → triggers.
        Triggers are left in Stopped state and must be activated manually.

        Args:
            artifacts_dir: Root output directory produced by the generators.
            dry_run: If True, validate files but do not call Azure APIs.

        Returns:
            List of DeployResult per artifact.
        """
        order = ["linkedService", "dataset", "dataflow", "pipeline", "trigger"]
        results: list[DeployResult] = []

        for artifact_type in order:
            sub_dir = artifacts_dir / artifact_type
            if not sub_dir.exists():
                continue
            for json_file in sorted(sub_dir.glob("*.json")):
                result = self._deploy_file(json_file, artifact_type, dry_run)
                results.append(result)

        return results

    def _deploy_file(
        self, json_file: Path, artifact_type: str, dry_run: bool
    ) -> DeployResult:
        name = json_file.stem
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return DeployResult(artifact_type=artifact_type, name=name, success=False,
                                error=f"Failed to read JSON: {exc}")

        if dry_run:
            logger.info("[DRY RUN] Would deploy %s: %s", artifact_type, name)
            return DeployResult(artifact_type=artifact_type, name=name, success=True)

        try:
            dispatch = {
                "linkedservice": self._deploy_linked_service,
                "dataset": self._deploy_dataset,
                "dataflow": self._deploy_data_flow,
                "pipeline": self._deploy_pipeline,
                "trigger": self._deploy_trigger,
            }
            fn = dispatch.get(artifact_type.lower())
            if fn is None:
                return DeployResult(artifact_type=artifact_type, name=name, success=False,
                                    error=f"Unknown artifact type: {artifact_type}")
            fn(name, payload)
            logger.info("Deployed %s: %s", artifact_type, name)
            return DeployResult(artifact_type=artifact_type, name=name, success=True)
        except Exception as exc:
            logger.error("Failed to deploy %s %s: %s", artifact_type, name, exc)
            return DeployResult(artifact_type=artifact_type, name=name, success=False,
                                error=str(exc))

    def _deploy_linked_service(self, name: str, payload: dict) -> None:
        resource = LinkedServiceResource(properties=payload.get("properties", payload))
        self.client.linked_services.create_or_update(
            self.resource_group, self.factory_name, name, resource
        )

    def _deploy_dataset(self, name: str, payload: dict) -> None:
        resource = DatasetResource(properties=payload.get("properties", payload))
        self.client.datasets.create_or_update(
            self.resource_group, self.factory_name, name, resource
        )

    def _deploy_data_flow(self, name: str, payload: dict) -> None:
        resource = DataFlowResource(properties=payload.get("properties", payload))
        self.client.data_flows.create_or_update(
            self.resource_group, self.factory_name, name, resource
        )

    def _deploy_pipeline(self, name: str, payload: dict) -> None:
        resource = PipelineResource(**payload.get("properties", payload))
        self.client.pipelines.create_or_update(
            self.resource_group, self.factory_name, name, resource
        )

    def _deploy_trigger(self, name: str, payload: dict) -> None:
        resource = TriggerResource(properties=payload.get("properties", payload))
        self.client.triggers.create_or_update(
            self.resource_group, self.factory_name, name, resource
        )
        # Leave in Stopped state — user must activate manually
        logger.info("Trigger %s deployed in Stopped state. Activate manually in ADF Studio.", name)

    def validate_artifacts(self, artifacts_dir: Path) -> list[dict[str, Any]]:
        """
        Validate JSON files in *artifacts_dir* against basic ADF schema requirements.
        Returns a list of validation issues (empty = all good).
        """
        import jsonschema  # type: ignore[import-untyped]

        issues: list[dict[str, Any]] = []
        for json_file in artifacts_dir.rglob("*.json"):
            try:
                payload = json.loads(json_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                issues.append({
                    "file": str(json_file),
                    "error": f"Invalid JSON: {exc}",
                })
                continue

            # Basic structural checks
            relative = json_file.relative_to(artifacts_dir)
            artifact_type = relative.parts[0].lower() if len(relative.parts) > 1 else "unknown"

            if "name" not in payload:
                issues.append({"file": str(json_file), "error": "Missing top-level 'name' field"})
            if "properties" not in payload:
                issues.append({"file": str(json_file), "error": "Missing top-level 'properties' field"})
            elif artifact_type == "pipeline" and "activities" not in payload.get("properties", {}):
                issues.append({"file": str(json_file), "error": "Pipeline missing 'activities' array"})

        issues.extend(_check_references(artifacts_dir))
        return issues


# ---------------------------------------------------------------------------
# Cross-artifact reference checks
# ---------------------------------------------------------------------------

_REF_FOLDERS = {
    "DatasetReference": "dataset",
    "LinkedServiceReference": "linkedService",
    "DataFlowReference": "dataflow",
    "PipelineReference": "pipeline",
}


def _iter_activity_lists(activities: list[Any]):
    yield activities
    for act in activities:
        tp = act.get("typeProperties", {}) if isinstance(act, dict) else {}
        for key in ("activities", "ifTrueActivities", "ifFalseActivities"):
            if isinstance(tp.get(key), list):
                yield from _iter_activity_lists(tp[key])
        for case in tp.get("cases", []) or []:
            if isinstance(case, dict) and isinstance(case.get("activities"), list):
                yield from _iter_activity_lists(case["activities"])


def _iter_references(node: Any):
    if isinstance(node, dict):
        ref_type = node.get("type")
        if ref_type in _REF_FOLDERS and isinstance(node.get("referenceName"), str):
            yield ref_type, node["referenceName"]
        for v in node.values():
            yield from _iter_references(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_references(v)


def _check_references(artifacts_dir: Path) -> list[dict[str, Any]]:
    """Find dangling activity dependencies and references to artifacts that
    were not generated (datasets, linked services, data flows, pipelines).

    References to artifacts that live outside *artifacts_dir* (e.g. shared
    linked services already deployed) are reported as warnings.
    """
    issues: list[dict[str, Any]] = []
    present: dict[str, set[str]] = {}
    payloads: list[tuple[Path, dict[str, Any]]] = []
    for json_file in artifacts_dir.rglob("*.json"):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rel = json_file.relative_to(artifacts_dir)
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        if isinstance(payload, dict) and "name" in payload:
            present.setdefault(folder, set()).add(payload["name"])
            payloads.append((json_file, payload))

    for json_file, payload in payloads:
        props = payload.get("properties", {})
        if not isinstance(props, dict):
            continue
        acts = props.get("activities")
        if isinstance(acts, list):
            for scope in _iter_activity_lists(acts):
                names = {a.get("name") for a in scope if isinstance(a, dict)}
                for a in scope:
                    if not isinstance(a, dict):
                        continue
                    for dep in a.get("dependsOn", []) or []:
                        target = dep.get("activity")
                        if target not in names:
                            issues.append({
                                "file": str(json_file),
                                "error": (
                                    f"Activity '{a.get('name')}' depends on '{target}', "
                                    "which is not an activity in the same scope"
                                ),
                            })
        for ref_type, ref_name in _iter_references(props):
            folder = _REF_FOLDERS[ref_type]
            if ref_name not in present.get(folder, set()):
                issues.append({
                    "file": str(json_file),
                    "severity": "warning",
                    "error": (
                        f"{ref_type} '{ref_name}' is not among the generated artifacts "
                        f"({folder}/); it must already exist in the factory"
                    ),
                })
    return issues
