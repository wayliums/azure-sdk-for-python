# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

# pylint: disable=protected-access

from contextlib import contextmanager
from os import PathLike, path
from typing import Dict, Iterable, Optional, Union
import uuid

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError
from marshmallow.exceptions import ValidationError as SchemaValidationError

from azure.ai.ml._artifacts._artifact_utilities import (
    _check_and_upload_path,
    _get_default_datastore_info,
    _update_metadata,
)
from azure.ai.ml._artifacts._constants import (
    ASSET_PATH_ERROR,
    CHANGED_ASSET_PATH_MSG,
    CHANGED_ASSET_PATH_MSG_NO_PERSONAL_DATA,
)
from azure.ai.ml._exception_helper import log_and_raise_error
from azure.ai.ml._restclient.v2021_10_01_dataplanepreview import (
    AzureMachineLearningWorkspaces as ServiceClient102021Dataplane,
)
from azure.ai.ml._restclient.v2022_10_01.models import ListViewType
from azure.ai.ml._restclient.v2023_04_01_preview import AzureMachineLearningWorkspaces as ServiceClient042023Preview
from azure.ai.ml._restclient.v2023_04_01_preview.models import (  # TODO: will need CustomAssetVersion in restclient contract to import from here
    ModelVersion,
)
from azure.ai.ml._scope_dependent_operations import (
    OperationConfig,
    OperationsContainer,
    OperationScope,
    _ScopeDependentOperations,
)
from ._code_operations import CodeOperations
from azure.ai.ml._telemetry import ActivityType, monitor_with_activity
from azure.ai.ml._utils._arm_id_utils import is_ARM_id_for_resource
from azure.ai.ml._utils._asset_utils import (
    _archive_or_restore,
    _get_latest,
    _get_next_version_from_container,
    _resolve_label_to_asset,
)
from azure.ai.ml._utils._experimental import experimental
from azure.ai.ml._utils._logger_utils import OpsLogger
from azure.ai.ml._utils._registry_utils import (
    get_asset_body_for_registry_storage,
    get_registry_client,
    get_sas_uri_for_registry_asset,
    get_storage_details_for_registry_assets,
)
from azure.ai.ml._utils._storage_utils import get_ds_name_and_path_prefix, get_storage_client
from azure.ai.ml._utils.utils import resolve_short_datastore_url, validate_ml_flow_folder
from azure.ai.ml.constants._common import ARM_ID_PREFIX, ASSET_ID_FORMAT, AzureMLResourceType
from azure.ai.ml.entities._assets._artifacts.code import Code
from azure.ai.ml.entities._assets._artifacts.custom_asset import CustomAsset
from azure.ai.ml.entities._assets.workspace_asset_reference import WorkspaceAssetReference
from azure.ai.ml.entities._credentials import AccountKeyConfiguration
from azure.ai.ml.exceptions import (
    AssetPathException,
    ErrorCategory,
    ErrorTarget,
    ValidationErrorType,
    ValidationException,
)
from azure.ai.ml.operations._datastore_operations import DatastoreOperations

from ._operation_orchestrator import OperationOrchestrator

ops_logger = OpsLogger(__name__)
logger, module_logger = ops_logger.package_logger, ops_logger.module_logger


class CustomAssetOperations(_ScopeDependentOperations):
    """CustomAssetOperations.

    You should not instantiate this class directly. Instead, you should create an MLClient instance that instantiates it
    for you and attaches it as an attribute.
    """

    # pylint: disable=unused-argument
    def __init__(
        self,
        operation_scope: OperationScope,
        operation_config: OperationConfig,
        service_client: Union[ServiceClient042023Preview, ServiceClient102021Dataplane],
        all_operations: OperationsContainer,
        credential: TokenCredential,
        **kwargs: Dict,
    ):
        super(CustomAssetOperations, self).__init__(operation_scope, operation_config)
        ops_logger.update_info(kwargs)
        # todo
        # self._custom_asset_versions_operation = service_client.custom_asset_versions
        # self._custom_asset_container_operation = service_client.custom_asset_containers
        self._service_client = service_client
        self._all_operations = all_operations
        self._credential = credential
        self._base_url = "http://10.120.210.85:57255/genericasset/v1.0/"

        # Maps a label to a function which given an asset name,
        # returns the asset associated with the label
        self._managed_label_resolver = {"latest": self._get_latest_version}

    # Upload code asset and return the asset ID
    def _upload_code(self, path: str) -> str:
        print('Uploading code from:', path)
        name = str(uuid.uuid4())
        version = '1'
        code = Code(name=name, version=version, path=path)
        result = self._code_operations.create_or_update(code)
        print('uploaded code asset:', result)
        print('returning code id:', result.id)
        return result.id
    
    @monitor_with_activity(logger, "CustomAsset.CreateOrUpdate", ActivityType.PUBLICAPI)
    def create_or_update(self, custom_asset: Union[CustomAsset, WorkspaceAssetReference]) -> CustomAsset:
        """Returns created or updated custom asset.

        :param custom_asset: Custom asset object.
        :type custom_asset: ~azure.ai.ml.entities._assets._artifacts.CustomAsset
        :raises ~azure.ai.ml.exceptions.AssetPathException: Raised when the CustomAsset artifact path is
            already linked to another asset
        :raises ~azure.ai.ml.exceptions.ValidationException: Raised if CustomAsset cannot be successfully validated.
            Details will be provided in the error message.
        :raises ~azure.ai.ml.exceptions.EmptyDirectoryError: Raised if local path provided points to an empty directory.
        :return: Custom asset object.
        :rtype: ~azure.ai.ml.entities._assets._artifacts.CustomAsset
        """
        ws_base_url = self._service_client.operations._client._base_url + "/.default"
        token = self._credential.get_token(ws_base_url).token

        data = {}
        data["RegistryName"] = self._registry_name
        data["Asset"] = {}
        data["Asset"]["Type"] = custom_asset.type
        if custom_asset.type_name:
            data["Asset"]["TypeName"] = custom_asset.type_name
        else:
            data["Asset"]["TypeName"] = custom_asset.type
        data["Asset"]["Name"] = custom_asset.name
        data["Asset"]["Version"] = custom_asset.version
        data["Asset"]["Description"] = custom_asset.description
        if custom_asset.implements:
            data["Asset"]["Implements"] = custom_asset.implements
        else:
            data["Asset"]["Implements"] = []
        data["Asset"]["AssetSpec"] = {}
        if custom_asset.inputs:
            data["Asset"]["AssetSpec"]["Inputs"] = custom_asset.inputs
        if custom_asset.template:
            if isinstance(custom_asset.template, str):
                data["Asset"]["AssetSpec"]["Template"] = custom_asset.template
            elif isinstance(custom_asset.template, dict):            
                if 'src' in custom_asset.template:
                    data["Asset"]["AssetSpec"]["Template"] = self._upload_code(custom_asset.template['src'])
                else:
                    print("Error: 'template' section specified without 'src'")
        if custom_asset.code:
            if isinstance(custom_asset.code, str):
                data["Asset"]["AssetSpec"]["Code"] = custom_asset.code
            elif isinstance(custom_asset.code, dict):            
                if 'src' in custom_asset.code:
                    data["Asset"]["AssetSpec"]["Code"] = self._upload_code(custom_asset.code['src'])
                else:
                    print("Error: 'code' section specified without 'src'")
            
        if custom_asset.environment:
            data["Asset"]["AssetSpec"]["Environment"] = custom_asset.environment
        import json

        import requests

        encoded_data = json.dumps(data).encode("utf-8")

        print("creating asset:")
        print(encoded_data)
        print()

        url = self._base_url + "create"
        s = requests.Session()
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        headers["Authorization"] = "Bearer " + token

        response = s.post(url, data=encoded_data, headers=headers)

        print("response code:", response.status_code)
        print("response content:", response.text)
        return custom_asset

    def _get(self, name: str, version: Optional[str] = None) -> "CustomAssetVersion":  # name:latest
        return NotImplementedError

    @monitor_with_activity(logger, "CustomAsset.Get", ActivityType.PUBLICAPI)
    def get(self, name: str, version: Optional[str] = None, label: Optional[str] = None) -> CustomAsset:
        """Returns information about the specified custom asset.

        :param name: Name of the custom asset.
        :type name: str
        :param version: Version of the custom asset.
        :type version: str
        :param label: Label of the custom asset. (mutually exclusive with version)
        :type label: str
        :raises ~azure.ai.ml.exceptions.ValidationException: Raised if Custom Asset cannot be successfully validated.
            Details will be provided in the error message.
        :return: Custom asset object.
        :rtype: ~azure.ai.ml.entities._assets._artifacts.CustomAsset
        """
        import json

        import requests

        ws_base_url = self._service_client.operations._client._base_url + "/.default"
        token = self._credential.get_token(ws_base_url).token

        url = self._base_url + "get"
        s = requests.Session()
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        headers["Authorization"] = "Bearer " + token

        data = {}
        data["AssetId"] = "azureml://registries/" + self._registry_name + "/assets/" + name + "/versions/" + version
        encoded_data = json.dumps(data).encode("utf-8")

        response = s.post(url, data=encoded_data, headers=headers)

        print("response code:", response.status_code)
        print("response content:", response.text)

        if response.status_code == 200:
            json_response = json.loads(response.text)
            
            name = json_response['name'] if 'name' in json_response else None
            version = json_response['version'] if 'version' in json_response else None
            type = json_response['type'] if 'type' in json_response else None
            type_name = json_response['typeName'] if 'typeName' in json_response else None
            description = json_response['description'] if 'description' in json_response else None
            implements = json_response['implements'] if 'implements' in json_response else None
            inputs = json_response['assetSpec']['Inputs'] if 'Inputs' in json_response['assetSpec'] else None
            template = json_response['assetSpec']['Template'] if 'Template' in json_response['assetSpec'] else None
            code = json_response['assetSpec']['Code'] if 'Code' in json_response['assetSpec'] else None
            environment = json_response['assetSpec']['Environment'] if 'Environment' in json_response['assetSpec'] else None
            
            asset = CustomAsset(name=name, version=version, type=type, type_name=type_name, description=description,
                                implements=implements, inputs=inputs, template=template, code=code, environment=environment)
            return asset
        return None

    @monitor_with_activity(logger, "CustomAsset.List", ActivityType.PUBLICAPI)
    def list(
        self,
        name: Optional[str] = None,
        stage: Optional[str] = None,
        *,
        list_view_type: ListViewType = ListViewType.ACTIVE_ONLY,
    ) -> Iterable[CustomAsset]:
        """List all custom assets in workspace.

        :param name: Name of the custom asset.
        :type name: Optional[str]
        :return: An iterator like instance of CustomAsset objects
        :rtype: ~azure.core.paging.ItemPaged[CustomAsset]
        """
        return NotImplementedError

    def _get_latest_version(self, name: str) -> ModelVersion:
        """Returns the latest version of the asset with the given name.

        Latest is defined as the most recently created, not the most recently updated.
        """
        return NotImplementedError

    @property
    def _code_operations(self) -> CodeOperations:
        return self._all_operations.get_operation(AzureMLResourceType.CODE, lambda x: isinstance(x, CodeOperations))
