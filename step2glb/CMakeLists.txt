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

# Embed an rpath so the binary can find the bundled OCCT dylibs at runtime
# regardless of CWD. The bundle script overrides install_names.
set_target_properties(step2glb PROPERTIES
    BUILD_RPATH "${OpenCASCADE_LIBRARY_DIR}"
)
