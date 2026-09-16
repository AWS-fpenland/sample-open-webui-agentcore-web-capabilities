# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""NAT plugin entry point: registers the AgentCore Code Interpreter sandbox provider with AI-Q at import."""

from ..sandbox_agentcore import register

register()
