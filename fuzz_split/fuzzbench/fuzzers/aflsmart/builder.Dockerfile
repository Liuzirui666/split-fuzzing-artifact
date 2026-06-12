# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

ARG parent_image
FROM $parent_image

# Install Python2 and build tools required by AFLSmart/Peach.
# gcc-4.4 no longer available in any repo; use default gcc instead.
RUN apt-get update && \
    apt-get install -y \
    gcc \
    g++ \
    unzip \
    wget \
    tzdata \
    python2 && \
    curl https://bootstrap.pypa.io/pip/2.7/get-pip.py --output get-pip.py && \
    python2 get-pip.py && \
    rm -f /usr/bin/python && \
    ln -s /usr/bin/python2.7 /usr/bin/python

# Install AFLSmart dependencies.
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y \
    apt-utils \
    libc6-dev-i386 \
    g++-multilib \
    mono-complete \
    software-properties-common

# Download and compile AFLSmart.
RUN git clone https://github.com/aflsmart/aflsmart /afl && \
    cd /afl && \
    git checkout 4286ae47e0e5d8c412f91aae94ef9d11fb97dfd8 && \
    AFL_NO_X86=1 make

# Setup Peach.
# Set CFLAGS="" so that we don't use the CFLAGS defined in OSS-Fuzz images.
# Use a copy of
# https://sourceforge.net/projects/peachfuzz/files/Peach/3.0/peach-3.0.202-source.zip
# to avoid network flakiness.
RUN cd /afl && \
    wget https://storage.googleapis.com/fuzzbench-files/peach-3.0.202-source.zip && \
    unzip peach-3.0.202-source.zip && \
    patch -p1 < peach-3.0.202.patch && \
    cd peach-3.0.202-source && \
    # Remove Pin analysis entirely (requires gcc-4.4 ABI, unavailable).
    rm -rf Peach.Core.Analysis.Pin.BasicBlocks Peach.Core.Analysis.Pin.CoverageEdge && \
    # Create stub dirs/wscripts so waf doesn't error on missing paths
    mkdir -p 3rdParty/pin Peach.Core.Analysis.Pin.BasicBlocks Peach.Core.Analysis.Pin.CoverageEdge && \
    echo "def build(bld): pass" > 3rdParty/pin/wscript && \
    echo "def build(bld): pass" > Peach.Core.Analysis.Pin.BasicBlocks/wscript && \
    echo "def build(bld): pass" > Peach.Core.Analysis.Pin.CoverageEdge/wscript && \
    CC=gcc CXX=g++ CFLAGS="" CXXFLAGS="-std=c++0x" ./waf configure && \
    CC=gcc CXX=g++ CFLAGS="" CXXFLAGS="-std=c++0x" ./waf install

# Use afl_driver.cpp from LLVM as our fuzzing library.
RUN wget https://raw.githubusercontent.com/llvm/llvm-project/5feb80e748924606531ba28c97fe65145c65372e/compiler-rt/lib/fuzzer/afl/afl_driver.cpp -O /afl/afl_driver.cpp && \
    clang -Wno-pointer-sign -c /afl/llvm_mode/afl-llvm-rt.o.c -I/afl && \
    clang++ -stdlib=libc++ -std=c++11 -O2 -c /afl/afl_driver.cpp && \
    ar r /libAFL.a *.o
