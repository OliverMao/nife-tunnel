import asyncio, sys, json
sys.path.insert(0, ".")
from nifetun.server import Edge

async def main():
    edge = Edge({
        "clients": [{"name": "nodetest", "token": "nodetok", "ids": ["*"], "hosts": ["*"]}],
        "public_listen": "127.0.0.1:18443",
        "client_listen": "127.0.0.1:18444",
        "admin_listen": "127.0.0.1:18445",
        "admin_token": "adm",
        "prefix": "/t", "prefix_hosts": ["h.test"],
        "window": 128 * 1024, "idle_timeout": 30, "access_log": True,
    })
    await edge.start()
    await edge.serve_forever()

asyncio.run(main())
