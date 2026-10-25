cmake_minimum_required(VERSION 3.24)
project(G3Together LANGUAGES C CXX)

if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 4)
  message(FATAL_ERROR "Use MSVC with -A Win32 for Gothic 3.")
endif()
# The unofficial Gothic 3 SDK (georgeto/gothic3sdk) is not part of this repository.
set(G3SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../g3-workspace/tools/gothic3sdk" CACHE PATH "Gothic 3 SDK checkout")
if(NOT EXISTS "${G3SDK_DIR}/cmake/Setup.cmake")
  message(FATAL_ERROR "Gothic 3 SDK not found at ${G3SDK_DIR}; pass -DG3SDK_DIR=<path>.")
endif()
set(G3SDK_BINDING_ONLY ON CACHE BOOL "Use only the SDK bindings" FORCE)
add_subdirectory("${G3SDK_DIR}" sdk)
include("${G3SDK_DIR}/cmake/Setup.cmake")

file(GLOB ENET_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/enet/*.c")
add_library(enet STATIC ${ENET_SOURCES})
target_include_directories(enet PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/enet/include")
target_compile_definitions(enet PRIVATE _WINSOCK_DEPRECATED_NO_WARNINGS)
# Gothic script targets default to __stdcall (/Gz); ENet must match so the import names agree.
target_compile_options(enet PRIVATE /Gz)
# compress.c only defines the range coder, whose functions are stored in ENET_CALLBACK (__cdecl) pointers;
# under /Gz they would be __stdcall and fail to compile. The mod never calls the range coder directly.
set_source_files_properties("${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/enet/compress.c" PROPERTIES COMPILE_OPTIONS /Gd)
target_link_libraries(enet PUBLIC ws2_32 winmm)

add_library(Script_G3Together SHARED src/Script_G3Together.cpp src/log.cpp src/qa.cpp src/config.cpp src/net.cpp
            src/remote.cpp src/body.cpp src/npcs.cpp src/ai.cpp src/combat.cpp src/hitprobe.cpp src/story.cpp src/talk.cpp src/items.cpp src/containers.cpp src/explore.cpp src/parity.cpp src/downed.cpp src/hud.cpp src/range.cpp src/journal.cpp src/persist.cpp src/hero.cpp src/clock.cpp src/infocmd.cpp src/transform.cpp src/ranged.cpp src/partner.cpp src/summon.cpp src/shot.cpp src/spells.cpp src/crashlog.cpp src/trace.cpp src/world.cpp src/social.cpp src/enclaves.cpp src/doors.cpp src/dialogaudio.cpp src/offer.cpp src/trade.cpp src/mapmarks.cpp src/ransack.cpp src/witness.cpp src/pickpocket.cpp src/occupancy.cpp src/guide.cpp src/i18n.cpp src/exports.def)
target_gothic_script_options(Script_G3Together)
# Sources are UTF-8: without /utf-8 MSVC reads them as Windows-1252 and HUD texts show "lÃ¤uft" (plan 3.5).
target_compile_options(Script_G3Together PRIVATE /utf-8)
# A map file, to turn crash addresses from the engine's report into functions.
target_link_options(Script_G3Together PRIVATE /MAP)
target_link_libraries(Script_G3Together PRIVATE g3sdk::SDK enet)
set_target_properties(Script_G3Together PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/artifacts")
