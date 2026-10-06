cmake_minimum_required(VERSION 3.20)
project(battlespades_steam_host LANGUAGES CXX)

# The Steam relay host for dedicated servers (see steam_host.cpp). It links
# the Steamworks SDK's redistributable library and ships beside it; the
# operator supplies Valve's steamclient runtime, as for the Steam sidecar.
#
# -DSTEAMWORKS_SDK_DIR=<dir> uses an SDK that is already on disk (the
# directory holding public/ and redistributable_bin/); otherwise the same
# pinned snapshot the BattleSpades client builds against is downloaded.
set(STEAMWORKS_SDK_DIR "" CACHE PATH "Steamworks SDK root (public/, redistributable_bin/)")
if(NOT STEAMWORKS_SDK_DIR)
    include(FetchContent)
    FetchContent_Declare(steamworks
        URL https://github.com/rlabrecque/SteamworksSDK/archive/df2baabf574a738ef1ea90a7e89339107fc0a279.tar.gz
        URL_HASH SHA256=4df87731a1179f20a9253a4348f6bd8381a8e46e5e3dc58f9db705ef200eea61
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
    FetchContent_MakeAvailable(steamworks)
    set(STEAMWORKS_SDK_DIR "${steamworks_SOURCE_DIR}")
endif()

string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" _arch)
if(MSVC_CXX_ARCHITECTURE_ID)
    string(TOLOWER "${MSVC_CXX_ARCHITECTURE_ID}" _arch)
endif()
set(_bin "${STEAMWORKS_SDK_DIR}/redistributable_bin")
if(WIN32)
    if(NOT _arch MATCHES "^(x64|amd64|x86_64)$")
        message(FATAL_ERROR "Valve ships no Steamworks library for Windows on ${_arch}")
    endif()
    set(_link "${_bin}/win64/steam_api64.lib")
    set(_runtime "${_bin}/win64/steam_api64.dll")
elseif(APPLE)
    set(_link "${_bin}/osx/libsteam_api.dylib")
    set(_runtime "${_link}")
elseif(_arch MATCHES "^(aarch64|arm64)$")
    set(_link "${_bin}/linuxarm64/libsteam_api.so")
    set(_runtime "${_link}")
else()
    set(_link "${_bin}/linux64/libsteam_api.so")
    set(_runtime "${_link}")
endif()
if(NOT EXISTS "${_link}")
    message(FATAL_ERROR "Steamworks library not found: ${_link}")
endif()

add_executable(battlespades-steam-host steam_host.cpp)
target_compile_features(battlespades-steam-host PRIVATE cxx_std_20)
target_include_directories(battlespades-steam-host SYSTEM PRIVATE "${STEAMWORKS_SDK_DIR}/public")
target_link_libraries(battlespades-steam-host PRIVATE "${_link}")
if(WIN32)
    target_link_libraries(battlespades-steam-host PRIVATE ws2_32)
    target_compile_definitions(battlespades-steam-host PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS)
else()
    find_package(Threads REQUIRED)
    target_link_libraries(battlespades-steam-host PRIVATE Threads::Threads)
    # The Steamworks library sits beside the executable.
    set_target_properties(battlespades-steam-host PROPERTIES
        BUILD_RPATH "$ORIGIN" INSTALL_RPATH "$ORIGIN" BUILD_WITH_INSTALL_RPATH TRUE)
endif()
if(MSVC)
    target_compile_options(battlespades-steam-host PRIVATE /W4 /permissive-)
else()
    target_compile_options(battlespades-steam-host PRIVATE -Wall -Wextra)
endif()

add_custom_command(TARGET battlespades-steam-host POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different "${_runtime}" "$<TARGET_FILE_DIR:battlespades-steam-host>"
    VERBATIM)
