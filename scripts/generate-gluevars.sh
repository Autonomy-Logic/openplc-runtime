#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

srcPATH=core/generated/plc_lib

./xml2st --generate-gluevars $srcPATH/LOCATED_VARIABLES.h
