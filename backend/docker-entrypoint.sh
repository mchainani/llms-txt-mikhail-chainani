#!/bin/sh
set -eu

role="${1:-server}"
if [ "$#" -gt 0 ]; then
    shift
fi

case "$role" in
    server)
        exec python -m uvicorn llms_txt_gen.main:app --host 0.0.0.0 --port 8000 \
            --log-config "${UVICORN_LOG_CONFIG:-logging.json}" "$@"
        ;;
    worker)
        exec python -m llms_txt_gen.worker_service "$@"
        ;;
    *)
        echo "Unknown role: $role. Expected server or worker." >&2
        exit 2
        ;;
esac
