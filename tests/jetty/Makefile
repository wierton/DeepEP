CANN_HOME ?= /usr/local/Ascend/cann
ASC ?= $(CANN_HOME)/tools/bisheng_compiler/bin/bisheng
CCE_FLAGS ?=
ROOT := ../..
ASC_ROOT := $(CANN_HOME)/asc
INCLUDES := -I$(ROOT)/deep_ep/include -I$(CANN_HOME)/include \
            -I$(ASC_ROOT) -I$(ASC_ROOT)/include -I$(ASC_ROOT)/include/basic_api \
            -I$(ASC_ROOT)/include/comm_api/aicore \
            -I$(CANN_HOME)/$(shell uname -m)-linux/ascendc/include/highlevel_api

.PHONY: compile
compile:
	mkdir -p $(ROOT)/build/jetty
	ASCEND_HOME_PATH=$(CANN_HOME) $(ASC) --asc-aicore-lang --npu-arch=dav-3510 --cce-aicore-only -std=c++17 -O2 $(CCE_FLAGS) $(INCLUDES) -c compile.asc -o $(ROOT)/build/jetty/kernels.o

.PHONY: host-writeback
host-writeback:
	mkdir -p $(ROOT)/build/jetty
	ASCEND_HOME_PATH=$(CANN_HOME) $(ASC) --asc-aicore-lang --npu-arch=dav-3510 -std=c++17 -O2 host_writeback.asc -lascendcl -o $(ROOT)/build/jetty/host_writeback

# After sourcing CANN set_env.sh, run build/jetty/host_writeback MODE:
# 0: compare addresses only; 1: SIMT mapped-host store;
# 2: scalar mapped-host store; 3: SIMT store with HostRegisterV2.
# Modes 1-3 reproduce a device fault on the tested 950PR environment.
