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
        # Render Web Services require an HTTP listener before deployment completes.
        # Serve only a health page, never the application directory.
        health_directory="$(mktemp -d)"
        printf 'ok\n' > "$health_directory/index.html"
        python -m http.server "${PORT:-10000}" --bind 0.0.0.0 \
            --directory "$health_directory" &
        exec procrastinate --verbose \
            --log-format "${LOG_FORMAT:-%(asctime)s %(levelname)s [%(name)s] %(message)s}" \
            --app=llms_txt_gen.jobs.app worker "$@"
        ;;
    *)
        echo "Unknown role: $role. Expected server or worker." >&2
        exit 2
        ;;
esac
