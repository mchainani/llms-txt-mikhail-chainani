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
        exec procrastinate --verbose \
            --log-format "${LOG_FORMAT:-%(asctime)s %(levelname)s [%(name)s] %(message)s}" \
            --app=llms_txt_gen.jobs.app worker "$@"
        ;;
    *)
        echo "Unknown role: $role. Expected server or worker." >&2
        exit 2
        ;;
esac
