cmake_minimum_required(VERSION 3.24)
project(autobench_cellerator_sparse_support LANGUAGES CXX CUDA)

if(NOT DEFINED CELLERATOR_SOURCE_DIR)
  message(FATAL_ERROR "Set CELLERATOR_SOURCE_DIR to the captured source snapshot")
endif()
get_filename_component(CELLERATOR_SOURCE_DIR "${CELLERATOR_SOURCE_DIR}" REALPATH)
set(CELLERATOR_INCLUDE_DIR "${CELLERATOR_SOURCE_DIR}/include")
find_package(CUDAToolkit REQUIRED)

add_library(cellerator_support STATIC
  "${CELLERATOR_SOURCE_DIR}/src/support/logical_support.cc"
  "${CELLERATOR_SOURCE_DIR}/src/support/adaptive_support.cc"
  "${CELLERATOR_SOURCE_DIR}/src/support/proposal_discovery.cc")
target_include_directories(cellerator_support PUBLIC "${CELLERATOR_INCLUDE_DIR}")
target_compile_features(cellerator_support PUBLIC cxx_std_17)

add_library(cellerator_indexing STATIC
  "${CELLERATOR_SOURCE_DIR}/src/indexing/reverse_postings.cc"
  "${CELLERATOR_SOURCE_DIR}/src/indexing/temporal_occurrences.cc")
target_include_directories(cellerator_indexing PUBLIC "${CELLERATOR_INCLUDE_DIR}")
target_link_libraries(cellerator_indexing PUBLIC cellerator_support)
target_compile_features(cellerator_indexing PUBLIC cxx_std_17)

add_library(cellerator_gating STATIC
  "${CELLERATOR_SOURCE_DIR}/src/gating/condition_program.cc"
  "${CELLERATOR_SOURCE_DIR}/src/gating/derivative_sampling.cc")
target_include_directories(cellerator_gating PUBLIC "${CELLERATOR_INCLUDE_DIR}")
target_link_libraries(cellerator_gating PUBLIC cellerator_support cellerator_indexing)
target_compile_features(cellerator_gating PUBLIC cxx_std_17)

add_library(cellerator_sparse_support_cuda STATIC
  "${CELLERATOR_SOURCE_DIR}/src/gating/cuda_worksets.cu"
  "${CELLERATOR_SOURCE_DIR}/src/indexing/cuda_postings.cu")
target_include_directories(cellerator_sparse_support_cuda PUBLIC "${CELLERATOR_INCLUDE_DIR}")
target_link_libraries(cellerator_sparse_support_cuda PUBLIC
  cellerator_gating cellerator_indexing CUDA::cudart)
set_target_properties(cellerator_sparse_support_cuda PROPERTIES
  CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED YES CUDA_ARCHITECTURES 70)
target_compile_options(cellerator_sparse_support_cuda PRIVATE
  "$<$<COMPILE_LANGUAGE:CUDA>:-lineinfo>")

add_executable(cellerator_sparse_support_cuda_indexing_bench
  "${CELLERATOR_SOURCE_DIR}/bench/sparse_support/cuda_indexing_bench.cu")
target_link_libraries(cellerator_sparse_support_cuda_indexing_bench PRIVATE
  cellerator_sparse_support_cuda cellerator_gating cellerator_indexing
  cellerator_support CUDA::cudart)
set_target_properties(cellerator_sparse_support_cuda_indexing_bench PROPERTIES
  CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED YES CUDA_ARCHITECTURES 70)
target_compile_options(cellerator_sparse_support_cuda_indexing_bench PRIVATE
  "$<$<COMPILE_LANGUAGE:CUDA>:-lineinfo>")
