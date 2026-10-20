#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Autonomy®

srcPATH=core/generated/plc_lib

./xml2st --generate-gluevars $srcPATH/LOCATED_VARIABLES.h
