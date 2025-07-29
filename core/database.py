import glob
import importlib
import os

from tortoise import Tortoise

from core.logger import logger
from core.settings import data_root


async def init_db():
    try:
        # 优化的SQLite配置
        db_url = f"sqlite://{data_root}/filecodebox.db"
        
        # SQLite性能优化配置
        db_config = {
            "db_url": db_url,
            "modules": {"models": ["apps.base.models"]},
            "use_tz": False,
            "timezone": "Asia/Shanghai",
            # 连接池配置
            "connections": {
                "default": {
                    "engine": "tortoise.backends.sqlite",
                    "credentials": {
                        "file_path": f"{data_root}/filecodebox.db",
                        # SQLite性能优化设置
                        "options": {
                            "init_command": [
                                "PRAGMA journal_mode=WAL;",           # WAL模式提高并发性能
                                "PRAGMA synchronous=NORMAL;",         # 平衡性能和安全性
                                "PRAGMA cache_size=10000;",           # 增加缓存大小(约40MB)
                                "PRAGMA temp_store=MEMORY;",          # 临时表存储在内存中
                                "PRAGMA mmap_size=268435456;",        # 启用内存映射(256MB)
                                "PRAGMA optimize;",                   # 自动优化
                            ]
                        }
                    }
                }
            }
        }

        await Tortoise.init(**db_config)

        # 创建migrations表
        await Tortoise.get_connection("default").execute_script("""
            CREATE TABLE IF NOT EXISTS migrates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                migration_file VARCHAR(255) NOT NULL UNIQUE,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 执行迁移
        await execute_migrations()
        
        # 应用性能优化设置
        await optimize_database()

    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}")
        raise


async def optimize_database():
    """应用数据库性能优化设置"""
    try:
        conn = Tortoise.get_connection("default")
        
        # 应用SQLite性能优化
        optimization_queries = [
            "PRAGMA journal_mode=WAL;",
            "PRAGMA synchronous=NORMAL;", 
            "PRAGMA cache_size=10000;",
            "PRAGMA temp_store=MEMORY;",
            "PRAGMA mmap_size=268435456;",
            "PRAGMA optimize;",
            # 启用查询规划器优化
            "PRAGMA query_only=0;",
            # 设置超时时间
            "PRAGMA busy_timeout=30000;",
        ]
        
        for query in optimization_queries:
            await conn.execute_query(query)
            
        logger.info("数据库性能优化设置已应用")
        
    except Exception as e:
        logger.warning(f"数据库优化设置失败: {str(e)}")


async def execute_migrations():
    """执行数据库迁移"""
    try:
        # 收集迁移文件
        migration_files = []
        for root, dirs, files in os.walk("apps"):
            if "migrations" in dirs:
                migration_path = os.path.join(root, "migrations")
                migration_files.extend(glob.glob(os.path.join(migration_path, "migrations_*.py")))

        # 按文件名排序
        migration_files.sort()

        for migration_file in migration_files:
            file_name = os.path.basename(migration_file)

            # 检查是否已执行
            executed = await Tortoise.get_connection("default").execute_query(
                "SELECT id FROM migrates WHERE migration_file = ?", [file_name]
            )

            if not executed[1]:
                logger.info(f"执行迁移: {file_name}")
                # 导入并执行migration
                module_path = migration_file.replace("/", ".").replace("\\", ".").replace(".py", "")
                try:
                    migration_module = importlib.import_module(module_path)
                    if hasattr(migration_module, "migrate"):
                        await migration_module.migrate()
                        # 记录执行
                        await Tortoise.get_connection("default").execute_query(
                            "INSERT INTO migrates (migration_file) VALUES (?)",
                            [file_name]
                        )
                        logger.info(f"迁移完成: {file_name}")
                except Exception as e:
                    logger.error(f"迁移 {file_name} 执行失败: {str(e)}")
                    raise

    except Exception as e:
        logger.error(f"迁移过程发生错误: {str(e)}")
        raise
