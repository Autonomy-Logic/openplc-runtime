#!/bin/bash
srcPATH=core/generated/plc_lib

./xml2st --generate-gluevars $srcPATH/LOCATED_VARIABLES.h
