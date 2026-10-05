"""Authenticated same-origin API backed by SQLite and an independent worker."""
from pathlib import Path
import sqlite3
from urllib.parse import quote, urlsplit
import uuid
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from . import batch_operations, catalog, core, db, enhancements, jobs, management, management_conditions, management_grpc, management_native, management_objects, management_ops, management_resources, storage, table_preview
from .config import Settings, get_settings, load_secret_registry
from .security import AppError, current_user, mutation, validate_origin


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.require_admin_password()
    db.initialize(settings)
    with db.connect(settings) as conn:
        core.bootstrap_admin(conn, settings)
        jobs.initialize(conn)
        catalog.initialize(conn)
        enhancements.initialize(conn)
        batch_operations.initialize(conn)
        management.initialize(conn)
        management_ops.initialize(conn)
        management_conditions.initialize(conn)
    app = FastAPI(title="SeaweedFS Console", version="0.1.0")
    app.state.settings = settings

    @app.middleware("http")
    async def headers(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(exc.to_response(getattr(request.state, "request_id", None)), status_code=exc.status)

    @app.exception_handler(storage.StorageError)
    async def storage_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)

    @app.exception_handler(enhancements.EnhancementError)
    async def enhancement_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=getattr(exc, "status", 400))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "参数不符合接口约定"}}, status_code=422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict_error(request, exc):
        return JSONResponse({"error": {"code": "RESOURCE_CONFLICT", "message": "资源重复或关联约束冲突"}}, status_code=409)

    @app.get("/health")
    def health():
        with db.connect(settings) as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "worker": "separate_process"}

    @app.post("/api/v1/auth/login")
    def login(request: Request, response: Response, body: dict):
        try:
            validate_origin(request.headers.get("origin"), settings, str(request.base_url).rstrip("/"))
        except PermissionError:
            raise AppError("FORBIDDEN_ORIGIN", "请求来源不受信任", 403)
        with db.connect(settings) as conn:
            result = core.login(conn, settings, body.get("username", ""), body.get("password", ""))
            previous = request.cookies.get(settings.session_cookie_name)
            if previous:
                try:
                    _, session = core.current_user_from_token(conn, previous)
                    core.logout(conn, session["id"])
                except AppError:
                    pass
        response.set_cookie(settings.session_cookie_name, result["session_token"], httponly=True,
                            secure=settings.effective_cookie_secure, samesite="lax", max_age=settings.session_ttl_seconds)
        return {"user": result["user"], "csrf_token": result["csrf_token"]}

    @app.get("/api/v1/auth/me")
    def me(request: Request):
        user = current_user(request)
        with db.connect(settings) as conn:
            return core.me(conn, user, request.state.session, settings)

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response):
        mutation(request)
        with db.connect(settings) as conn:
            core.logout(conn, request.state.session["id"])
        response.delete_cookie(settings.session_cookie_name, secure=settings.effective_cookie_secure, httponly=True, samesite="lax")
        return {"logged_out": True}

    @app.get("/api/v1/connections")
    def connections(request: Request):
        current_user(request)
        with db.connect(settings) as conn:
            return {"items": core.list_connections(conn)}

    @app.post("/api/v1/connections")
    def connection_create(request: Request, body: dict):
        mutation(request)
        parsed = urlsplit(body.get("endpoint_url", ""))
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AppError("INVALID_ENDPOINT", "需无凭据的HTTP(S)批准存储地址", 422)
        if body.get("addressing_style", "path") not in ("path", "virtual"):
            raise AppError("INVALID_REQUEST", "寻址类型无效", 422)
        entry = load_secret_registry(settings).get(body.get("secret_ref"))
        if not isinstance(entry, dict) or not entry.get("access_key_id") or not entry.get("secret_access_key"):
            raise AppError("SECRET_REF_NOT_FOUND", "请先在后端注册凭据引用", 403)
        body = dict(body)
        body["display_name"] = body.get("display_name") or body.get("name")
        with db.connect(settings) as conn:
            result = core.create_connection(conn, body)
            storage.secret_for_connection(settings, core._connection_private(conn, result["id"]))
            return result

    @app.post("/api/v1/connections/{connection_id}/probe")
    def probe(request: Request, connection_id: str, body: dict | None = None):
        mutation(request)
        body = body or {}
        with db.connect(settings) as conn:
            return core.probe_connection(conn, settings, connection_id, bucket=body.get("bucket"), prefix=body.get("prefix", ""))

    @app.get("/api/v1/connections/{connection_id}/capabilities")
    def capabilities(request: Request, connection_id: str):
        current_user(request)
        with db.connect(settings) as conn:
            return core.get_connection(conn, connection_id)

    @app.get("/api/v1/projects")
    def projects(request: Request):
        current_user(request)
        with db.connect(settings) as conn:
            return {"items": core.list_projects(conn)}

    @app.post("/api/v1/projects")
    def project_create(request: Request, body: dict):
        mutation(request)
        with db.connect(settings) as conn:
            return core.create_project(conn, body)

    @app.get("/api/v1/projects/{project_id}/scopes")
    def scopes(request: Request, project_id: str):
        current_user(request)
        with db.connect(settings) as conn:
            return {"items": core.list_scopes(conn, project_id)}

    @app.post("/api/v1/projects/{project_id}/scopes")
    def scope_create(request: Request, project_id: str, body: dict):
        mutation(request)
        with db.connect(settings) as conn:
            return core.create_scope(conn, project_id, body)

    @app.post("/api/v1/scopes/{scope_id}/archive")
    def archive(request: Request, scope_id: str):
        mutation(request)
        with db.connect(settings) as conn:
            return core.archive_scope(conn, scope_id)

    @app.get("/api/v1/scopes/{scope_id}/objects")
    def objects(request: Request, scope_id: str, limit: int = 60, cursor: str | None = None):
        scope = core.require_scope(request, scope_id)
        with db.connect(settings) as conn:
            return catalog.query_catalog(conn, settings, scope, catalog._filters(request), limit, cursor)

    @app.get("/api/v1/scopes/{scope_id}/objects/{object_id}")
    def detail(request: Request, scope_id: str, object_id: str):
        return core.require_object(request, scope_id, object_id)

    @app.get("/api/v1/scopes/{scope_id}/objects/{object_id}/download")
    def download(request: Request, scope_id: str, object_id: str, version_id: str | None = None):
        core.require_scope(request, scope_id)
        with db.connect(settings) as conn:
            result = core.download_object(conn, settings, scope_id, object_id, requested_version_id=version_id)
        return StreamingResponse(result.chunks, media_type="application/octet-stream", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(result.filename), "Cache-Control": "no-store"})

    app.include_router(catalog.router)
    app.include_router(jobs.router)
    app.include_router(batch_operations.router)
    app.include_router(management.router)
    app.include_router(management_resources.router)
    app.include_router(management_native.router)
    app.include_router(management_objects.router)
    app.include_router(management_conditions.router)
    app.include_router(management_grpc.router)
    app.include_router(table_preview.router)
    jobs.HANDLERS["copy_batch"] = batch_operations.execute_copy_batch
    try:
        from . import enhancement_api
        app.include_router(enhancement_api.router)
        jobs.HANDLERS["derived_variant"] = enhancement_api.execute_variant_job
    except ImportError as exc:
        if "enhancement_api" not in str(exc):
            raise
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        @app.get("/{path:path}")
        def frontend(path: str):
            if path.startswith("api/"):
                raise AppError("NOT_FOUND", "接口不存在", 404)
            target = (dist / path).resolve()
            if not target.is_relative_to(dist.resolve()):
                raise AppError("NOT_FOUND", "文件不存在", 404)
            return FileResponse(target if target.is_file() else dist / "index.html")
    return app


