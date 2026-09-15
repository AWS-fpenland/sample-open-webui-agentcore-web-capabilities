# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AI-Q on Amazon Bedrock AgentCore — runtime adapter package.

This package is the AWS-side adapter around the upstream NVIDIA AI-Q Blueprint
(Apache-2.0, https://github.com/NVIDIA-AI-Blueprints/aiq). It owns identity,
durable job/event state, the AgentCore Runtime entrypoint, the Bedrock model
configuration, and the AgentCore-native tool adapters. Upstream AI-Q agent code
is consumed as a pinned dependency and is not modified here.
"""

__version__ = "0.1.0"
