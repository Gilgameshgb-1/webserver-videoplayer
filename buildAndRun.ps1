$BuildDir = "build"
$DepsDir = "deps"
$ExecutableName = "wserVideoPlayer.exe"

conan install . --output-folder=$BuildDir --build=missing

cmake -S . -B $BuildDir `
    -G "MinGW Makefiles" `
    -DCMAKE_TOOLCHAIN_FILE="$BuildDir/conan_toolchain.cmake" `
    -DCMAKE_BUILD_TYPE=Release

cmake --build $BuildDir

Get-ChildItem -Path $DepsDir -Filter *.dll -Recurse | Copy-Item -Destination $BuildDir -Force

& build/.\$ExecutableName
