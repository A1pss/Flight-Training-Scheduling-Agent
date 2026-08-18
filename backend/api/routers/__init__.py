"""HTTP 路由。

**这里不 re-export 子模块**（M4-B §8 第 1 条的同一条约定）：
`from backend.api.routers import chat` 显式写全，避免 import 副作用把
六个路由模块的依赖一次性拉进任何 import 过本包的进程。
"""
