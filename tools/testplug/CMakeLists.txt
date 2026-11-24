# ArtCore Test Plug — a tiny VST3 effect for ArtCore Sound's tests (not shipped):
# gain, any channel count 1–8 (in = out), a fixed-latency variant, and on purpose a crash or a hang,
# so that the plugin host's isolation can be tested for real.
# Build: cmake -S tools/testplug -B build/testplug -DVST3_SDK=<path to vst3sdk> && cmake --build build/testplug --config Release
cmake_minimum_required(VERSION 3.25.0)
project(ArtCoreTestPlug VERSION 1.0.0)

set(VST3_SDK "" CACHE PATH "The VST 3 SDK (github.com/steinbergmedia/vst3sdk, MIT)")
set(SMTG_ENABLE_VSTGUI_SUPPORT OFF CACHE BOOL "" FORCE)
set(SMTG_ENABLE_VST3_PLUGIN_EXAMPLES OFF CACHE BOOL "" FORCE)
set(SMTG_ENABLE_VST3_HOSTING_EXAMPLES OFF CACHE BOOL "" FORCE)
set(SMTG_RUN_VST_VALIDATOR OFF CACHE BOOL "" FORCE)
set(SMTG_CREATE_PLUGIN_LINK OFF CACHE BOOL "" FORCE)
set(SMTG_ADD_VSTGUI OFF CACHE BOOL "" FORCE)
add_subdirectory(${VST3_SDK} vst3sdk)
smtg_enable_vst3_sdk()

smtg_add_vst3plugin(ArtCoreTestPlug
    ${VST3_SDK}/public.sdk/source/vst/vstsinglecomponenteffect.cpp
    ${VST3_SDK}/public.sdk/source/vst/vstsinglecomponenteffect.h
    testplug.cpp)
target_link_libraries(ArtCoreTestPlug PRIVATE sdk)
