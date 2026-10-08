"""The relay between the website and Modal. Deploy it once: `modal deploy relay.py`.

A web page cannot talk to Modal's API directly (it is gRPC, with no CORS), so the site sends the
player's Modal token here and the relay acts with it:

  POST /api/launch    deploy session_app.py into the player's workspace (first time: builds the
                      image, about 5-10 minutes), stop any session already running, start one
  POST /api/job       progress of a launch, then the session's address and key
  POST /api/sessions  the player's running sessions
  POST /api/stop      stop all of the player's sessions
  GET  /api/health    is the relay up

Sessions run, and are billed, in the workspace of the token that started them. The relay itself
costs its owner a few cents a month: it scales to zero and each call takes seconds.

The token is used for the request and then dropped: it is never logged or stored here. A launch
hands it to a short background job in this app (so a first launch can outlive the 150-second
limit on web requests), which also forgets it when done.
"""

import hashlib
import secrets
import time

import modal

import session_app

app = modal.App("roblox-relay")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install(f"modal=={modal.__version__}", "fastapi[standard]==0.115.12")
    .add_local_python_source("session_app")
)

progress = modal.Dict.from_name("roblox-relay-progress", create_if_missing=True)

MAX_CPU = 32
JOB_HOURS = 1


def owner_tag(token_id: str) -> str:
    return hashlib.sha256(token_id.encode()).hexdigest()[:16]


def user_client(token_id: str, token_secret: str) -> modal.Client:
    if not token_id.startswith("ak-") or not token_secret.startswith("as-"):
        raise ValueError("A Modal token ID starts with ak- and its secret with as-.")
    return modal.Client.from_credentials(token_id, token_secret)


def memory_for(cpu: float) -> int:
    """MiB: Roblox itself needs about 3 GiB; Mesa's renderer threads add a little per core."""
    return int(min(32768, 4096 + 512 * cpu))


@app.function(image=image, timeout=JOB_HOURS * 3600)
def launch(token_id: str, token_secret: str, opts: dict) -> dict:
    job = modal.current_function_call_id()

    def say(text: str) -> None:
        progress[job] = {"text": text, "at": time.time()}

    client = user_client(token_id, token_secret)
    registry = modal.Dict.from_name(session_app.REGISTRY_NAME, create_if_missing=True, client=client)

    say("Stopping any session that is still running")
    stop_all(client, registry)

    if registry.get("__version__") != session_app.VERSION or not deployed(client):
        say("Setting up your Modal workspace. The first time, this builds the Roblox image: "
            "about 5-10 minutes. Later launches skip this.")
        session_app.app.deploy(client=client)
        registry["__version__"] = session_app.VERSION

    say("Starting a container")
    cpu = float(opts["cpu"])
    fn = modal.Function.from_name(session_app.APP_NAME, "session", client=client).with_options(
        cpu=cpu, memory=memory_for(cpu), timeout=int(opts["max_hours"] * 3600)
    )
    sid = secrets.token_hex(8)
    key = secrets.token_urlsafe(24)
    registry[sid] = {"status": "starting", "key": key, "mode": opts["mode"], "cpu": cpu,
                     "started": time.time(), "call_id": None}
    call = fn.spawn(sid, key, opts["mode"], opts["idle_minutes"], opts["width"], opts["height"])
    registry[sid] = {**registry[sid], "call_id": call.object_id}

    deadline = time.time() + 300
    while time.time() < deadline:
        entry = registry.get(sid) or {}
        if entry.get("url"):
            say("Session started")
            return {"owner": owner_tag(token_id), "session": public(sid, entry)}
        try:
            call.get(timeout=2)  # returns only if the session already ended
            raise RuntimeError("The session ended before it started; check the app's logs on modal.com.")
        except TimeoutError:
            pass
    call.cancel(terminate_containers=True)
    registry.pop(sid, None)
    raise RuntimeError("Modal did not start the container within 5 minutes. Try again.")


def deployed(client: modal.Client) -> bool:
    try:
        modal.Function.from_name(session_app.APP_NAME, "session", client=client).hydrate()
        return True
    except modal.exception.NotFoundError:
        return False


def public(sid: str, entry: dict) -> dict:
    return {"id": sid, "url": entry["url"], "key": entry["key"], "mode": entry.get("mode"),
            "cpu": entry.get("cpu"), "started": entry.get("started")}


def running(client: modal.Client, registry: modal.Dict) -> dict:
    """The registry's sessions that are still running; stale entries are dropped."""
    alive = {}
    for sid, entry in list(registry.items()):
        if sid.startswith("__"):
            continue
        call_id = entry.get("call_id")
        if call_id:
            try:
                modal.FunctionCall.from_id(call_id, client=client).get(timeout=0)
                registry.pop(sid, None)  # finished
                continue
            except TimeoutError:
                pass  # still running
            except Exception:
                registry.pop(sid, None)  # failed or cancelled
                continue
        elif time.time() - entry.get("started", 0) > 600:
            registry.pop(sid, None)
            continue
        alive[sid] = entry
    return alive


def stop_all(client: modal.Client, registry: modal.Dict) -> int:
    stopped = 0
    for sid, entry in list(registry.items()):
        if sid.startswith("__"):
            continue
        if entry.get("call_id"):
            try:
                modal.FunctionCall.from_id(entry["call_id"], client=client).cancel(terminate_containers=True)
                stopped += 1
            except Exception:
                pass
        registry.pop(sid, None)
    return stopped


@app.function(image=image, scaledown_window=120)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def web():
    import asyncio

    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field

    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
                       allow_headers=["content-type"])

    class Creds(BaseModel):
        token_id: str = Field(max_length=200)
        token_secret: str = Field(max_length=200)

    class Launch(Creds):
        cpu: float = Field(8, ge=1, le=MAX_CPU)
        mode: str = Field("pc", pattern="^(pc|mobile)$")
        idle_minutes: float = Field(10, ge=2, le=120)
        max_hours: float = Field(3, gt=0, le=24)
        width: int = Field(1280, ge=640, le=1920)
        height: int = Field(720, ge=360, le=1080)

    class Job(BaseModel):
        job: str = Field(max_length=100)
        token_id: str = Field(max_length=200)

    def with_client(body: Creds):
        try:
            client = user_client(body.token_id.strip(), body.token_secret.strip())
            registry = modal.Dict.from_name(session_app.REGISTRY_NAME, create_if_missing=True, client=client)
            registry.hydrate()
            return client, registry
        except (modal.exception.AuthError, ValueError) as e:
            raise HTTPException(401, f"Modal did not accept that token. {e}".strip())

    async def in_thread(fn):
        """Run blocking Modal calls off the event loop; any failure becomes a readable 502.

        (An unhandled exception would reach the browser as a 500 without CORS headers, which a
        page cannot read at all.)
        """
        try:
            return await asyncio.to_thread(fn)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Modal error: {e or type(e).__name__}")

    @api.get("/api/health")
    async def health():
        return {"ok": True}

    @api.post("/api/launch")
    async def launch_session(body: Launch):
        await in_thread(lambda: with_client(body))  # fail fast on a bad token
        opts = body.model_dump(exclude={"token_id", "token_secret"})
        call = await launch.spawn.aio(body.token_id.strip(), body.token_secret.strip(), opts)
        return {"job": call.object_id}

    @api.post("/api/job")
    async def job_status(body: Job):
        call = modal.FunctionCall.from_id(body.job)
        try:
            result = await call.get.aio(timeout=0)
        except TimeoutError:
            note = await progress.get.aio(body.job) or {}
            return {"state": "running", "progress": note.get("text", "Waiting for Modal")}
        except Exception as e:
            await progress.pop.aio(body.job, None)
            return {"state": "error", "error": str(e) or type(e).__name__}
        if result["owner"] != owner_tag(body.token_id.strip()):
            raise HTTPException(403, "That launch belongs to a different token.")
        await progress.pop.aio(body.job, None)
        return {"state": "done", "session": result["session"]}

    @api.post("/api/sessions")
    async def list_sessions(body: Creds):
        def work():
            client, registry = with_client(body)
            return [public(sid, e) for sid, e in running(client, registry).items() if e.get("url")]

        return {"sessions": await in_thread(work)}

    @api.post("/api/stop")
    async def stop_sessions(body: Creds):
        def work():
            client, registry = with_client(body)
            return stop_all(client, registry)

        return {"stopped": await in_thread(work)}

    return api
