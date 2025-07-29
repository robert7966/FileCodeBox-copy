# @Time    : 2023/8/9 23:23
# @Author  : Lan
# @File    : main.py
# @Software: PyCharm
import asyncio
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from tortoise.contrib.fastapi import register_tortoise

from apps.base.models import KeyValue
from apps.base.utils import ip_limit
from apps.base.views import share_api, chunk_api
from apps.admin.views import admin_api
from core.database import init_db
from core.response import APIResponse
from core.settings import data_root, settings, BASE_DIR, DEFAULT_CONFIG
from core.tasks import delete_expire_files
from core.logger import logger
from core.performance import performance_cleanup_task, performance_monitor, get_performance_recommendations

from contextlib import asynccontextmanager
from tortoise import Tortoise


class OptimizedStaticFiles(StaticFiles):
    """Optimized static files with better caching headers"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        
        # Add aggressive caching for static assets
        if any(response.path.name.endswith(ext) for ext in ['.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf']):
            # Cache static assets for 1 year
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            response.headers["Expires"] = "Thu, 31 Dec 2037 23:55:55 GMT"
        
        # Add security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化应用...")
    # 初始化数据库
    await init_db()

    # 加载配置
    await load_config()
    app.mount(
        "/assets",
        OptimizedStaticFiles(directory=f"./{settings.themesSelect}/assets"),
        name="assets",
    )

    # 启动后台任务
    file_cleanup_task = asyncio.create_task(delete_expire_files())
    perf_cleanup_task = asyncio.create_task(performance_cleanup_task())
    
    logger.info("应用初始化完成")

    try:
        yield
    finally:
        # 清理操作
        logger.info("正在关闭应用...")
        file_cleanup_task.cancel()
        perf_cleanup_task.cancel()
        await asyncio.gather(file_cleanup_task, perf_cleanup_task, return_exceptions=True)
        await Tortoise.close_connections()
        logger.info("应用已关闭")


async def load_config():
    user_config, _ = await KeyValue.get_or_create(
        key="settings", defaults={"value": DEFAULT_CONFIG}
    )
    await KeyValue.update_or_create(
        key="sys_start", defaults={"value": int(time.time() * 1000)}
    )
    settings.user_config = user_config.value
    # 更新 ip_limit 配置
    ip_limit["error"].minutes = settings.errorMinute
    ip_limit["error"].count = settings.errorCount
    ip_limit["upload"].minutes = settings.uploadMinute
    ip_limit["upload"].count = settings.uploadCount


app = FastAPI(lifespan=lifespan)

# Add GZip compression middleware (should be added before other middleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 使用 register_tortoise 来添加异常处理器
register_tortoise(
    app,
    config={
        "connections": {"default": f"sqlite://{data_root}/filecodebox.db"},
        "apps": {
            "models": {
                "models": ["apps.base.models"],
                "default_connection": "default",
            },
        },
    },
    generate_schemas=False,
    add_exception_handlers=True,
)

app.include_router(share_api)
app.include_router(chunk_api)
app.include_router(admin_api)


@app.exception_handler(404)
@app.get("/")
async def index(request=None, exc=None):
    response = HTMLResponse(
        content=open(
            BASE_DIR / f"{settings.themesSelect}/index.html", "r", encoding="utf-8"
        )
        .read()
        .replace("{{title}}", str(settings.name))
        .replace("{{description}}", str(settings.description))
        .replace("{{keywords}}", str(settings.keywords))
        .replace("{{opacity}}", str(settings.opacity))
        .replace('"/assets/', '"assets/')
        .replace("{{background}}", str(settings.background)),
        media_type="text/html",
    )
    
    # Add cache control for HTML (short cache)
    response.headers["Cache-Control"] = "public, max-age=300"  # 5 minutes
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    
    return response


@app.get("/robots.txt")
async def robots():
    response = HTMLResponse(content=settings.robotsText, media_type="text/plain")
    response.headers["Cache-Control"] = "public, max-age=86400"  # 1 day
    return response


@app.post("/")
async def get_config():
    return APIResponse(
        detail={
            "name": settings.name,
            "description": settings.description,
            "explain": settings.page_explain,
            "uploadSize": settings.uploadSize,
            "expireStyle": settings.expireStyle,
            "enableChunk": settings.enableChunk if settings.file_storage == "local" and settings.enableChunk else 0,
            "openUpload": settings.openUpload,
            "notify_title": settings.notify_title,
            "notify_content": settings.notify_content,
            "show_admin_address": settings.showAdminAddr,
            "max_save_seconds": settings.max_save_seconds,
        }
    )


@app.get("/performance/report")
async def get_performance_report():
    """获取性能报告 - 管理员接口"""
    report = performance_monitor.get_performance_report()
    recommendations = get_performance_recommendations()
    
    return APIResponse(detail={
        **report,
        "recommendations": recommendations
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app="main:app", host="0.0.0.0", port=settings.port, reload=False, workers=1
    )
