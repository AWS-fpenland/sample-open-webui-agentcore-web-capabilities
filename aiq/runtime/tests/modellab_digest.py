# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test helper: the same canonical digest the Model Lab publishes (kept here so runtime tests need no aiq/modellab import)."""
import hashlib
import json


def digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
