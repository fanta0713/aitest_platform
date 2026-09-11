"""
FastAPI 应用入口
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os

from app.db.database import init_db
from app.api import tasks, auth, users, cases, caselib, products, files, export, suites


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库
    await init_db()
    yield
    # 关闭时的清理工作


app = FastAPI(
    title="测试任务管理系统",
    description="测试任务全生命周期管理平台",
    version="3.0.0",
    lifespan=lifespan,
)

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(tasks.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(cases.router)
app.include_router(caselib.router)
app.include_router(products.router)
app.include_router(files.router)
app.include_router(export.router)
app.include_router(suites.router)


# 前端静态文件
static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """根路径返回前端页面（禁缓存）"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(
            index_path,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
    return {
        "message": "测试任务管理系统 API",
        "docs": "/docs",
        "version": "2.0.0",
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "service": "testtask-backend"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
