cmake -DCMAKE_INSTALL_PREFIX=${PREFIX} -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_LIBDIR=lib ${CMAKE_ARGS} -DCMAKE_CUDA_ARCHITECTURES=all ${SRC_DIR}
cmake --build . --target install --verbose
cmake --build . --target docs --verbose
