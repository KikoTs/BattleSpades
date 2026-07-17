cmake_minimum_required(VERSION 3.20)
project(battlespades_steam_bridge LANGUAGES CXX)

if(NOT WIN32)
    message(FATAL_ERROR "The recovered SteamGameServer011 runtime is Windows x86")
endif()

add_executable(battlespades-steam-bridge steam_bridge.cpp)
target_compile_features(battlespades-steam-bridge PRIVATE cxx_std_17)
target_compile_definitions(
    battlespades-steam-bridge
    PRIVATE
        NOMINMAX
        WIN32_LEAN_AND_MEAN
        _CRT_SECURE_NO_WARNINGS
)
if(MSVC)
    target_compile_options(battlespades-steam-bridge PRIVATE /W4 /permissive-)
    set_property(
        TARGET battlespades-steam-bridge
        PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )
endif()

