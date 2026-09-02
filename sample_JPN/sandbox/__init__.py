"""The Agent Smith sandbox: secure execution boundary + MCP client (Section 4.2).

【日本語解説】
sandbox/ パッケージのトップレベル。中身は executor.py（サンドボックス本体）、
isolated_process.py / isolated_worker.py（OSレベル隔離の親子プロセス）、
mcp_client.py（MCPToolProxy）、cli.py（対話REPL）の5ファイル。
このパッケージがプロジェクト全体の中で最もセキュリティ上重要な部分。
"""
