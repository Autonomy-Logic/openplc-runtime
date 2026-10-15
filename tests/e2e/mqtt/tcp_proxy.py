# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Plain TCP forwarder; stopping its container cuts every connection through it at once."""

from __future__ import annotations

import asyncio
import sys


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _handle(
    client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter, host: str, port: int
) -> None:
    try:
        upstream_r, upstream_w = await asyncio.open_connection(host, port)
    except OSError:
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, upstream_w), _pipe(upstream_r, client_w))


async def main(listen: int, host: str, port: int) -> None:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, host, port), host="0.0.0.0", port=listen
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], int(sys.argv[3])))
