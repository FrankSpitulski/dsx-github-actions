// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

module cds-golangci-lint-compat

go 1.27.3

require (
	github.com/golangci/golangci-lint v1.64.8
	golang.org/x/tools v0.52.0
	// Go 1.27 syntax and export-data support for staticcheck and unused.
	honnef.co/go/tools v0.8.2
)
