#!/bin/sh
# System packages of the weekly build on Ubuntu 24.04 (GitHub's ubuntu-24.04 runner, or a local machine).
# Run as root (sudo sh build/setup_ubuntu.sh). Python 3.12 is Ubuntu 24.04's own python3.
#   python3.12-venv            the builders' virtual environments
#   openjdk-25-jdk-headless    Java for Planetiler and Maven (the map part)
#   git                        build_profile.py checks out the pinned Protomaps profile
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3.12-venv openjdk-25-jdk-headless git ca-certificates >/dev/null
python3.12 --version
java -version 2>&1 | head -n 1
