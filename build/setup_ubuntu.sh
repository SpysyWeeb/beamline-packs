#!/bin/sh
# System packages of the weekly build on Ubuntu 24.04 (GitHub's ubuntu-24.04 runner, a self-hosted
# runner or a local machine: the same script for each). Run as root (sudo sh build/setup_ubuntu.sh).
# Python 3.12 is Ubuntu 24.04's own python3.
#   python3.12-venv            the builders' virtual environments
#   openjdk-25-jdk-headless    Java for Planetiler and Maven (the map part)
#   git                        build_profile.py checks out the pinned Protomaps profile; mapd.py setup
#                              fetches Mapd's source at the pinned StarPilot commit
#   gcc, pkg-config,           Mapd's build (the comma files, packbuilder/comma/mapd.py setup): cgo needs
#   zlib1g-dev                 a C compiler, and Mapd's gzip library asks pkg-config for zlib's headers
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3.12-venv openjdk-25-jdk-headless git ca-certificates \
  gcc libc6-dev pkg-config zlib1g-dev >/dev/null
python3.12 --version
java -version 2>&1 | head -n 1
gcc --version | head -n 1
pkg-config --modversion zlib
