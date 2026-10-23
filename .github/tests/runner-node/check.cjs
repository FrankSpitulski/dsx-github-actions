// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

console.log(`Runner-bundled Node: ${process.version} (${process.execPath})`);
const [major, minor, patch] = process.versions.node.split('.').map(Number);
if (major !== 24 || minor < 22 || (minor === 22 && patch < 1)) {
  console.error('Update the Actions runner to one bundling Node.js 24.22.1 or newer in the Node 24 line.');
  process.exitCode = 1;
}
