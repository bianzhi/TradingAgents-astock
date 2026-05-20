#!/bin/sh
set -e

# Fix ownership of mounted volume — Docker volumes are owned by root by default.
mkdir -p /home/appuser/.tradingagents/cache \
         /home/appuser/.tradingagents/logs \
         /home/appuser/.tradingagents/memory
chown -R appuser:appuser /home/appuser/.tradingagents

# Drop to appuser and exec the CMD
exec su -s /bin/sh appuser -c '"$0" "$@"' -- "$@"
