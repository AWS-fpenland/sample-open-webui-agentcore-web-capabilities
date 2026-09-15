"""Offline version-pin wire contract and failure tests; never simulate a deploy.

PYTHONPATH="$ASSET" python3 -B -m unittest discover -s gateway/web-search-provisioner -v
Native CFN ownership, TargetId wiring, and rollback policies are asserted in
infra/test/web-capabilities.test.ts. These tests exercise only the pin handler;
they do not claim live CloudFormation rollback verification.
"""

import copy
import datetime
import importlib.util
import io
from pathlib import Path
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import boto3
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.stub import Stubber
from botocore.validate import validate_parameters


spec = importlib.util.spec_from_file_location("web_search_provider", Path(__file__).with_name("index.py"))
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class VersionPinTests(unittest.TestCase):
    def setUp(self):
        self.event = {
            "RequestType": "Create", "RequestId": "request-1",
            "ResourceProperties": {
                "GatewayIdentifier": "gateway-123", "TargetId": "target-123",
                "TargetName": "web-search-tool", "ConnectorId": "web-search", "ConnectorVersion": "1.2.0",
            },
        }
        self.event["OldResourceProperties"] = copy.deepcopy(self.event["ResourceProperties"])
        self.physical_id = "web-search-version/gateway-123/target-123"
        self.target = {
            "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gateway-123",
            "targetId": "target-123", "status": "READY", "name": "web-search-tool",
            "description": "Native CFN target",
            "createdAt": datetime.datetime(2026, 9, 15, tzinfo=datetime.timezone.utc),
            "updatedAt": datetime.datetime(2026, 9, 15, tzinfo=datetime.timezone.utc),
            "targetConfiguration": {"mcp": {"connector": {
                "source": {"connectorId": "web-search", "version": "1.1.0"},
                "enabled": ["WebSearch"],
                "configurations": [{"name": "WebSearch", "parameterValues": {}}],
            }}},
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        }
        self.ready = copy.deepcopy(self.target)
        self.ready["targetConfiguration"]["mcp"]["connector"]["source"]["version"] = "1.2.0"
        self.control = Mock()
        self.control.get_gateway_target.side_effect = [self.target, self.ready]
        self.client_patch = patch.object(provider, "control", self.control)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.sleep_patch = patch.object(provider.time, "sleep")
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def call(self, request_type="Create"):
        self.event["RequestType"] = request_type
        if request_type != "Create":
            self.event.setdefault("PhysicalResourceId", self.physical_id)
        return provider.handler(self.event, None)

    def error(self, code="ResourceNotFoundException"):
        return ClientError({"Error": {"Code": code, "Message": "private service payload"}}, "GetGatewayTarget")

    def assert_no_ownership_calls(self):
        self.control.create_gateway_target.assert_not_called()
        self.control.delete_gateway_target.assert_not_called()
        self.control.list_gateway_targets.assert_not_called()

    def test_pinned_sdk_stubber_validates_exact_get_update_contract(self):
        self.assertEqual(boto3.__version__, "1.43.94")
        client = boto3.client("bedrock-agentcore-control", region_name="us-east-1",
                              aws_access_key_id="test", aws_secret_access_key="test")
        identity = {"gatewayIdentifier": "gateway-123", "targetId": "target-123"}
        expected = {**identity, "name": "web-search-tool", "description": "Native CFN target",
                    "targetConfiguration": self.ready["targetConfiguration"],
                    "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]}
        with Stubber(client) as stubber, patch.object(provider, "control", client):
            stubber.add_response("get_gateway_target", self.target, identity)
            stubber.add_response("update_gateway_target", self.ready, expected)
            stubber.add_response("get_gateway_target", self.ready, identity)
            result = self.call()
            stubber.assert_no_pending_responses()
        self.assertEqual(result, {"PhysicalResourceId": self.physical_id, "Data": {
            "TargetId": "target-123", "ToolName": "web-search-tool___WebSearch", "ConnectorVersion": "1.2.0",
        }})

    def test_create_retry_is_idempotent_when_already_pinned(self):
        self.control.get_gateway_target.side_effect = [self.ready, self.ready]
        self.assertEqual(self.call()["PhysicalResourceId"], self.physical_id)
        self.assertEqual(self.call()["PhysicalResourceId"], self.physical_id)
        self.control.update_gateway_target.assert_not_called()
        self.assert_no_ownership_calls()

    def test_update_is_idempotent(self):
        self.control.get_gateway_target.side_effect = [self.target, self.ready, self.ready]
        self.assertEqual(self.call("Update")["PhysicalResourceId"], self.physical_id)
        self.call("Update")
        self.control.update_gateway_target.assert_called_once()
        self.assert_no_ownership_calls()

    def test_preserves_exact_nested_parameters_credentials_and_optional_settings(self):
        configuration = self.target["targetConfiguration"]["mcp"]["connector"]["configurations"][0]
        configuration["description"] = "Keep this tool description"
        configuration["parameterValues"] = {"domainFilter": {"exclude": ["example.com"]}, "extension": [1, False, {"key": "value"}]}
        configuration["parameterOverrides"] = [{"path": "maxResults", "description": "Preserve override", "visible": False}]
        self.target["metadataConfiguration"] = {"allowedResponseHeaders": ["x-search-id"]}
        self.target["privateEndpoint"] = {"selfManagedLatticeResource": {
            "resourceConfigurationIdentifier": "arn:aws:vpc-lattice:us-east-1:123456789012:resourceconfiguration/rcfg-0123456789abcdef0",
        }}
        original = copy.deepcopy(self.target)
        pinned = copy.deepcopy(original)
        pinned["targetConfiguration"]["mcp"]["connector"]["source"]["version"] = "1.2.0"
        self.control.get_gateway_target.side_effect = [self.target, pinned]
        self.call()
        actual = self.control.update_gateway_target.call_args.kwargs
        client = boto3.client("bedrock-agentcore-control", region_name="us-east-1",
                              aws_access_key_id="test", aws_secret_access_key="test")
        validate_parameters(actual, client.meta.service_model.operation_model("UpdateGatewayTarget").input_shape)
        for key in ["name", "description", "targetConfiguration", "credentialProviderConfigurations", "metadataConfiguration", "privateEndpoint"]:
            self.assertEqual(actual[key], pinned[key])
        self.assertEqual(self.target, original)

    def test_does_not_send_readonly_response_fields_to_update(self):
        self.target["authorizationData"] = [{"opaque": "read-only"}]
        self.call()
        self.assertEqual(set(self.control.update_gateway_target.call_args.kwargs), {
            "gatewayIdentifier", "targetId", "name", "description", "targetConfiguration", "credentialProviderConfigurations",
        })

    def test_waits_for_initial_readiness_and_resolved_pin(self):
        self.control.get_gateway_target.side_effect = [
            self.error(), {**self.target, "status": "CREATING"}, self.target,
            {**self.ready, "status": "UPDATING"}, self.target, self.ready,
        ]
        self.call()
        self.assertEqual(self.sleep.call_count, 4)

    def test_update_conflicts_are_retried_within_a_bound(self):
        self.control.update_gateway_target.side_effect = [self.error("ConflictException"), self.error("ConflictException"), self.ready]
        self.call()
        self.assertEqual(self.control.update_gateway_target.call_count, provider.UPDATE_ATTEMPTS)

    def test_exhausted_conflicts_fail_without_owning_cleanup(self):
        self.control.update_gateway_target.side_effect = self.error("ConflictException")
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual(self.control.update_gateway_target.call_count, provider.UPDATE_ATTEMPTS)
        self.assert_no_ownership_calls()

    def test_ambiguous_update_response_can_be_recovered_by_retry(self):
        self.control.update_gateway_target.side_effect = ReadTimeoutError(endpoint_url="https://example.invalid")
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual(self.call()["PhysicalResourceId"], self.physical_id)
        self.control.update_gateway_target.assert_called_once()
        self.assert_no_ownership_calls()

    def test_failed_custom_create_without_framework_delete_leaves_cleanup_to_native_cfn(self):
        self.control.get_gateway_target.side_effect = [self.target, {**self.ready, "status": "FAILED"}]
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual([call[0] for call in self.control.mock_calls], [
            "get_gateway_target", "update_gateway_target", "get_gateway_target",
        ])
        self.assert_no_ownership_calls()

    def test_retried_create_still_creating_is_bounded_and_cannot_strand_provider_owned_target(self):
        self.control.get_gateway_target.side_effect = None
        self.control.get_gateway_target.return_value = {**self.target, "status": "CREATING"}
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.call()
        self.assertEqual(self.control.get_gateway_target.call_count, provider.POLL_ATTEMPTS * 2)
        self.control.update_gateway_target.assert_not_called()
        self.assert_no_ownership_calls()

    def test_ready_wrong_pin_does_not_report_success(self):
        self.control.get_gateway_target.side_effect = [self.target] * (provider.POLL_ATTEMPTS + 1)
        with self.assertRaises(RuntimeError):
            self.call()
        self.assert_no_ownership_calls()

    def test_parameter_drift_during_pin_is_not_false_success(self):
        changed = copy.deepcopy(self.ready)
        changed["targetConfiguration"]["mcp"]["connector"]["configurations"][0]["parameterValues"] = {"unexpected": True}
        self.control.get_gateway_target.side_effect = [self.target] + [changed] * provider.POLL_ATTEMPTS
        with self.assertRaises(RuntimeError):
            self.call()

    def test_wall_clock_deadline_bounds_readiness(self):
        self.control.get_gateway_target.side_effect = [{**self.target, "status": "CREATING"}]
        with patch.object(provider.time, "monotonic", side_effect=[0, provider.WAIT_SECONDS + 1]):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                provider._wait("gateway-123", "target-123")
        self.assertEqual(self.control.get_gateway_target.call_count, 1)

    def test_unsuccessful_update_can_be_repaired_on_rollback(self):
        self.control.get_gateway_target.side_effect = [{**self.ready, "status": "UPDATE_UNSUCCESSFUL"}, self.ready]
        self.call("Update")
        self.control.update_gateway_target.assert_called_once()

    def test_delete_is_unconditional_noop_even_without_properties(self):
        for physical_id in [self.physical_id, "failed-create-placeholder", "old-gateway/old-target"]:
            result = provider.handler({"RequestType": "Delete", "PhysicalResourceId": physical_id}, None)
            self.assertEqual(result, {"PhysicalResourceId": physical_id})
        provider.handler({"RequestType": "Delete"}, None)
        self.assertEqual(self.control.mock_calls, [])

    def test_gateway_and_target_replacements_are_rejected_before_sdk_access(self):
        for key in ["GatewayIdentifier", "TargetId"]:
            with self.subTest(key=key):
                original = self.event["ResourceProperties"][key]
                self.event["ResourceProperties"][key] = "replacement-123"
                with self.assertRaises(RuntimeError):
                    self.call("Update")
                self.event["ResourceProperties"][key] = original
        self.assertEqual(self.control.mock_calls, [])

    def test_update_rejects_mismatched_physical_identity(self):
        self.event["PhysicalResourceId"] = "web-search-version/old-gateway/old-target"
        with self.assertRaises(RuntimeError):
            self.call("Update")
        self.assertEqual(self.control.mock_calls, [])

    def test_update_requires_old_properties(self):
        del self.event["OldResourceProperties"]
        with self.assertRaises(RuntimeError):
            self.call("Update")
        self.assertEqual(self.control.mock_calls, [])

    def test_native_identity_and_connector_mismatch_fail_closed(self):
        wrong_connector = copy.deepcopy(self.target)
        wrong_connector["targetConfiguration"]["mcp"]["connector"]["source"]["connectorId"] = "not-search"
        for target in [{**self.target, "name": "someone-else"}, {**self.target, "targetId": "another-target"}, wrong_connector]:
            self.control.get_gateway_target.side_effect = [target]
            with self.assertRaises(RuntimeError):
                self.call()
        self.control.update_gateway_target.assert_not_called()
        self.assert_no_ownership_calls()

    def test_unsupported_properties_and_requests_do_not_call_sdk(self):
        for key in ["TargetName", "ConnectorId", "ConnectorVersion", "GatewayIdentifier", "TargetId"]:
            original = self.event["ResourceProperties"][key]
            self.event["ResourceProperties"][key] = ""
            with self.assertRaises(RuntimeError):
                self.call()
            self.event["ResourceProperties"][key] = original
        with self.assertRaises(RuntimeError):
            self.call("Read")
        self.assertEqual(self.control.mock_calls, [])

    def test_access_denied_is_not_treated_as_missing(self):
        self.control.get_gateway_target.side_effect = self.error("AccessDeniedException")
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual(self.control.get_gateway_target.call_count, 1)

    def test_error_tracebacks_do_not_expose_raw_payloads(self):
        self.event["RawSecret"] = "private-event-payload"
        self.control.get_gateway_target.side_effect = self.error("AccessDeniedException")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            try:
                self.call()
            except RuntimeError:
                traceback.print_exc()
        self.assertNotIn("private-event-payload", output.getvalue())
        self.assertNotIn("private service payload", output.getvalue())
        self.assertIn("Search version pin failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
