#!/usr/bin/env bash
# Запуск depgraph.
#   ./run.sh test            - запустить тесты
#   ./run.sh <аргументы>     - запустить программу, например:
#   ./run.sh -p A -r examples/repo.txt -t -a
set -e
cd "$(dirname "$0")"

if [ "$1" = "test" ]; then
    python3 -m unittest discover -s tests -v
else
    python3 src/main.py "$@"
fi
