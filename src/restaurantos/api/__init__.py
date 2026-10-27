"""HTTP and WebSocket surface: the registries (``registry``), FastAPI dependencies (``deps``), error rendering
(``errors``) and one ``routes_*`` module per owning package (CONTRACTS_API §3, §7). This package imports nothing on
its own, so route modules never pull in each other."""
