# Builds the HMI end-to-end test's board plugin; scripts/compile.sh passes INCLUDE_DIRS and OUTPUT_DIR.
CC ?= gcc
OUTPUT_DIR ?= .

all: $(OUTPUT_DIR)/libe2evpp_plugin.so

$(OUTPUT_DIR)/libe2evpp_plugin.so: e2e_vpp_plugin.c
	$(CC) -shared -fPIC -O2 -Wall -Wextra $(INCLUDE_DIRS) -o $@ $<

.PHONY: all
