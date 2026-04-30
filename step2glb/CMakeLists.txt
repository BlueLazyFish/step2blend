cmake_minimum_required(VERSION 3.20)
project(step2glb CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(OpenCASCADE REQUIRED)

add_executable(step2glb step2glb.cpp)

target_include_directories(step2glb PRIVATE
    ${OpenCASCADE_INCLUDE_DIR}
)
target_link_directories(step2glb PRIVATE
    ${OpenCASCADE_LIBRARY_DIR}
)

target_link_libraries(step2glb PRIVATE
    TKDESTEP TKDEGLTF
    TKXCAF TKLCAF TKCAF TKCDF
    TKRWMesh TKMesh
    TKBO TKBRep TKGeomBase TKGeomAlgo TKTopAlgo TKPrim TKShHealing
    TKG3d TKG2d TKMath
    TKXSBase
    TKBinXCAF TKBinL TKBin
    TKernel
)

# macOS: embed an rpath so the binary finds OCCT dylibs at runtime no
# matter the CWD. bundle_macos.sh later overrides the install_names so
# the shipped copy looks at @executable_path/../lib instead.
#
# Windows: DLLs are resolved by being in the same directory as the EXE,
# so no rpath equivalent is needed — bundle_windows.ps1 puts the OCCT
# DLLs alongside step2glb.exe. Linux would use RUNPATH (not yet shipped).
if(APPLE)
    set_target_properties(step2glb PROPERTIES
        BUILD_RPATH "${OpenCASCADE_LIBRARY_DIR}"
    )
endif()
