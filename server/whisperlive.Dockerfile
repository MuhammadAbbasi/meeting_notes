# WhisperLive with working GPU inference.
# The upstream image ships torch 2.14 with CUDA 13 libraries, but its ctranslate2 4.9 wheel is built
# for CUDA 12 and fails every chunk with "Library libcublas.so.12 is not found". Add the CUDA 12
# cuBLAS + cuDNN wheels in a side folder and put them first on the library path; torch keeps loading
# its own CUDA 13 libraries by absolute path, so both coexist.
FROM ghcr.io/collabora/whisperlive-gpu:latest
RUN pip install --no-cache-dir --target /opt/cu12 "nvidia-cublas-cu12>=12.4,<13" "nvidia-cudnn-cu12>=9,<10"
ENV LD_LIBRARY_PATH=/opt/cu12/nvidia/cublas/lib:/opt/cu12/nvidia/cudnn/lib:${LD_LIBRARY_PATH}
