#!/bin/bash
set -e

BUILD_DIR="build"
DEPS_DIR="deps"
EXECUTABLE_NAME="wserVideoPlayer"

conan install . --output-folder="$BUILD_DIR" --build=missing

cmake -S . -B "$BUILD_DIR" \
    -G "Unix Makefiles" \
    -DCMAKE_TOOLCHAIN_FILE="$BUILD_DIR/conan_toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Release

cmake --build "$BUILD_DIR"
