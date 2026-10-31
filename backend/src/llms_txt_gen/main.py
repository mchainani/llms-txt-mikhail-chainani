from fastapi import FastAPI

from llms_txt_gen.routers.files import router as files_router
from llms_txt_gen.routers.requests import router as requests_router
from llms_txt_gen.routers.runs import router as runs_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="llms-txt-gen",
        version="0.1.0",
        description="Create and inspect llms.txt requests, generation runs, and generated files.",
    )
    app.include_router(runs_router)
    app.include_router(requests_router)
    app.include_router(files_router)

    return app


app = create_app()
