#!/bin/bash
# Setup script for installing the harness agent inside a TB2 container.
# This gets copied into the container and executed.

set -e

apt-get update -qq
apt-get install -y -qq python3 python3-pip git > /dev/null 2>&1

# Copy agent code (mounted or pre-copied to /opt/harness-agent)
mkdir -p /opt/harness-agent
cd /opt/harness-agent

# Install Python dependencies
pip3 install -q openai tiktoken

echo "Harness agent installed successfully."
