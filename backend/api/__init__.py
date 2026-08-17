"""FastAPI 接入层（v6 §9）。

**这里不 re-export 任何子模块**（M4-B §8 第 1 条）：`backend.api.main` 会拉起
路由、Redis 客户端与图依赖，让 `import backend.api` 变成一次重量级 import
是没必要的。要什么显式写全路径。
"""
