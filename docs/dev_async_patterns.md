# Threading Model

This document explains how the middleware handles concurrency, and what that means when
extending it.

## The Codebase Is Synchronous

Service methods, P2P protocol handlers, and the `Node` lifecycle are all plain synchronous
functions. `Node.startup()` and `Node.shutdown()` are called directly — there is no
`asyncio.run()` at CLI entry points, and no event loop in the main thread.

Concurrency comes from threads, not from an event loop.

The only async code left is at the REST boundary:

| Location | Why it is async |
|----------|-----------------|
| `simaas/rest/auth.py` | FastAPI dependency callables, which FastAPI awaits |
| `simaas/rest/service.py` | The uvicorn server coroutine and the FastAPI lifespan hook |

## Daemon Threads

Both long-running servers run in their own daemon thread, started by `Node.startup()`:

```
Node.startup()            <-- synchronous
  --> P2PService.start_service_background()
        daemon thread running _ThreadedSSLServer.serve_forever()
  --> RESTService.start_service()
        daemon thread running asyncio.run(uvicorn_server.serve())
```

The REST event loop is created inside its own thread and never escapes it, so nothing
elsewhere in the codebase has to be event-loop aware.

### P2P: One Thread per Connection

`_ThreadedSSLServer` is a `socketserver.ThreadingTCPServer` with `daemon_threads = True`
and `request_queue_size = 128`. Each inbound connection is handled in a fresh thread, which
reads the request, dispatches it to the registered `P2PProtocol`, writes the reply, and exits.

The practical consequence: **`P2PProtocol.handle()` may be called concurrently on the same
protocol instance.** Protocol handlers and the services they call must be thread-safe.

## Implications When Extending

- Blocking is fine. Synchronous database calls, file I/O, and `subprocess` are all expected
  in service methods and protocol handlers; they block only their own thread.
- Do not add `async def` to service interfaces or `P2PProtocol` subclasses. Nothing awaits
  them, so a coroutine would be returned and silently never run.
- Guard mutable shared state. `Keystore` uses a `threading.Lock` for this; follow the same
  pattern in plugins that hold state across calls.
- Protocol instances are long-lived. `get_p2p_protocols()` is called once at startup, and the
  returned instances serve every subsequent request, from many threads.

## Key Files

| File | Role |
|------|------|
| `simaas/p2p/service.py` | P2P daemon thread; thread-per-connection dispatch |
| `simaas/rest/service.py` | REST daemon thread; the one `asyncio.run()` in the codebase |
| `simaas/node/base.py` | Starts and stops both daemon threads |
