# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------
from os import PathLike
from pathlib import Path
from typing import Any, Dict, Optional, Union

from azure.ai.ml._schema import CustomAssetSchema, ModelSchema
from azure.ai.ml._utils._arm_id_utils import AMLNamedArmId, AMLVersionedArmId
from azure.ai.ml._utils._asset_utils import get_ignore_file, get_object_hash
from azure.ai.ml.constants._common import (
    BASE_PATH_CONTEXT_KEY,
    LONG_URI_FORMAT,
    PARAMS_OVERRIDE_KEY,
    ArmConstants,
    AssetTypes,
)
from azure.ai.ml.entities._assets import Artifact
from azure.ai.ml.entities._assets.intellectual_property import IntellectualProperty
from azure.ai.ml.entities._system_data import SystemData
from azure.ai.ml.entities._util import get_md5_string, load_from_dict

from .artifact import ArtifactStorageInfo


class CustomAsset(Artifact):  # pylint: disable=too-many-instance-attributes
    """Custom Asset for training and scoring.

    :param name: Name of the resource.
    :type name: str
    :param version: Version of the resource.
    :type version: str
    :param type: TODO: define this
    :type type: str
    :param path: A remote uri or a local path pointing at a model.
        Example: "azureml://subscriptions/{}/resourcegroups/{}/workspaces/{}/datastores/{}/paths/path_on_datastore/"
    :type path: str
    :param description: Description of the resource.
    :type description: str
    :param tags: Tag dictionary. Tags can be added, removed, and updated.
    :type tags: dict[str, str]
    :param properties: The asset property dictionary.
    :type properties: dict[str, str]
    :param stage: The stage of the resource.
    :type stage: str
    :param implements: Specifies which interfaces the asset implements. Possible values include: "deployable", "pipeline_runnable".
    :type implements: Optional[list[str]]
    :param kwargs: A dictionary of additional configuration parameters.
    :type kwargs: dict
    """

    def __init__(
        self,
        *,
        name: Optional[str] = None,
        version: Optional[str] = None,
        type: Optional[str] = None,  # pylint: disable=redefined-builtin
        type_name: Optional[str] = None,
        path: Optional[Union[str, PathLike]] = None,
        description: Optional[str] = None,
        tags: Optional[Dict] = None,
        properties: Optional[Dict] = None,
        stage: Optional[str] = None,
        implements: Optional[list[str]] = None,
        inputs: Optional[Dict] = None,
        template: Optional[Union[str,Dict]] = None,
        code: Optional[Union[str,Dict]] = None,
        environment: Optional[str] = None,
        **kwargs,
    ):
        self._intellectual_property = kwargs.pop("intellectual_property", None)
        self.inputs = inputs
        self.template = template
        self.code = code
        self.environment = environment
        self.type_name = type_name
        super().__init__(
            name=name,
            version=version,
            path=path,
            description=description,
            tags=tags,
            properties=properties,
            **kwargs,
        )
        self.type = type
        self.stage = stage
        self.implements = implements
        # Are there any other attributes we need here?
        if self._is_anonymous and self.path:
            _ignore_file = get_ignore_file(self.path)
            _upload_hash = get_object_hash(self.path, _ignore_file)
            self.name = get_md5_string(_upload_hash)

    @classmethod
    def _load(
        cls,
        data: Optional[Dict] = None,
        yaml_path: Optional[Union[PathLike, str]] = None,
        params_override: Optional[list] = None,
        **kwargs,
    ) -> "CustomAsset":
        params_override = params_override or []
        data = data or {}
        context = {
            BASE_PATH_CONTEXT_KEY: Path(yaml_path).parent if yaml_path else Path("./"),
            PARAMS_OVERRIDE_KEY: params_override,
        }
        return load_from_dict(CustomAssetSchema, data, context, **kwargs)
        # return NotImplementedError

    def _to_dict(self) -> Dict:
        return CustomAssetSchema(context={BASE_PATH_CONTEXT_KEY: "./"}).dump(self)  # pylint: disable=no-member
        # return NotImplementedError

    @classmethod
    def _from_rest_object(cls, custom_asset_rest_object: "CustomAssetVersion") -> "CustomAsset":
        raise NotImplementedError

    @classmethod
    def _from_container_rest_object(cls, custom_asset_container_rest_object: "CustomAssetContainer") -> "CustomAsset":
        return NotImplementedError

    def _to_rest_object(self) -> "CustomAssetVersion":
        raise NotImplementedError

    def _update_path(self, asset_artifact: ArtifactStorageInfo) -> None:
        # datastore_arm_id is null for registry scenario, so capture the full_storage_path
        if not asset_artifact.datastore_arm_id and asset_artifact.full_storage_path:
            self.path = asset_artifact.full_storage_path
        else:
            aml_datastore_id = AMLNamedArmId(asset_artifact.datastore_arm_id)
            self.path = LONG_URI_FORMAT.format(
                aml_datastore_id.subscription_id,
                aml_datastore_id.resource_group_name,
                aml_datastore_id.workspace_name,
                aml_datastore_id.asset_name,
                asset_artifact.relative_path,
            )

    def _to_arm_resource_param(self, **kwargs):  # pylint: disable=unused-argument
        properties = self._to_rest_object().properties

        return {
            self._arm_type: {
                ArmConstants.NAME: self.name,
                ArmConstants.VERSION: self.version,
                ArmConstants.PROPERTIES_PARAMETER_NAME: self._serialize.body(properties, "ModelVersionProperties"),
            }
        }
