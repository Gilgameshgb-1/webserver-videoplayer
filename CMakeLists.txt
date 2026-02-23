cmake_minimum_required(VERSION 3.15)
project(WebServerVideoPlayer CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# fmt by conan
find_package(fmt REQUIRED)

# mpv lib for windows from deps
set(MPV_DIR "${CMAKE_SOURCE_DIR}/deps")
find_library(MPV_LIB NAMES mpv libmpv PATHS ${MPV_DIR})

add_executable(wserVideoPlayer main.cpp)

# Inclusions
target_include_directories(wserVideoPlayer PRIVATE "${MPV_DIR}/include")

# Linking
target_link_libraries(wserVideoPlayer PRIVATE fmt::fmt ${MPV_LIB})