VULKAN_SDK ?= $(HOME)/VulkanSDK/1.4.363.0/macOS
CXX := clang++
CXXFLAGS := -std=c++17 -O2 -Wall -Wextra -Werror

.PHONY: all clean selftest

all: build/vk_run build/mtl_run

build/vk_run: host/vk_run.cpp | build
	$(CXX) $(CXXFLAGS) -Wno-missing-field-initializers -I$(VULKAN_SDK)/include $< -o $@ \
		-L$(VULKAN_SDK)/lib -lvulkan -Wl,-rpath,$(VULKAN_SDK)/lib

build/mtl_run: host/mtl_run.mm | build
	$(CXX) $(CXXFLAGS) -fobjc-arc $< -o $@ -framework Metal -framework Foundation

selftest: all
	python3 -m difftest.run selftest

build:
	mkdir -p $@

clean:
	rm -rf build work
