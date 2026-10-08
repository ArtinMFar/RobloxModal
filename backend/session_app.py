"""The Modal app that runs one Roblox session, deployed into the player's own Modal workspace.

The relay (relay.py) deploys this app with the player's token and then calls `session`, so the
player's workspace pays for it. Nothing here runs on a GPU: Modal's GPU containers have no
/dev/dri, so Roblox's renderer falls back to Mesa's llvmpipe on the CPU either way.

Inside the container:

  cage     headless Wayland compositor, patched (below) with a touchscreen fed from a socket
           and a settable screen size
    -> cordial-run -> Roblox's Android client (Cordial, GPL-3.0, github.com/luohoa97/cordial)
  wayvnc   VNC server on the compositor, 127.0.0.1:5900
  this     one HTTP port, published with `modal.forward` (TLS):
             GET  /status    JSON for the site's loading screen
             WS   /vnc       the screen plus keyboard and mouse (VNC, bridged to wayvnc)
             WS   /touch     mobile mode: fingers, passed to cage's touchscreen
             POST /restart   start Roblox again
             POST /shutdown  end the session

Every request must carry the session key (`?k=`) that only the browser that launched the
session was given. The session ends itself after RM idle minutes with nobody watching, and at
the latest when the function's timeout runs out.
"""

import base64
import os
import time

import modal

APP_NAME = "roblox-modal"
VOLUME_NAME = "roblox-modal-data"  # the Roblox Android build, downloaded on first use
REGISTRY_NAME = "roblox-modal-sessions"  # running sessions, so the site can find and stop them
VERSION = "10"  # bump when this file changes, so the relay redeploys it

CORDIAL_VERSION = "0.27.0"  # the first release whose touch input does not crash
CORDIAL_DEB = (
    f"https://github.com/luohoa97/cordial/releases/download/v{CORDIAL_VERSION}/"
    f"cordial_{CORDIAL_VERSION}-1_amd64.deb"
)

CAGE_COMMIT = "8a009212bcc7d7766e1f1601605a3ae923a84b1a"  # cage 0.1.5+20240127, as in Ubuntu 24.04

# Added to cage's seat.c, just before seat_create(), which then calls rm_virtual_touch(seat).
CAGE_TOUCH_C = r'''
/* ---- robloxmodal: a touchscreen driven over a Unix socket ---------------------------------
 *
 * With CAGE_VIRTUAL_TOUCH=<path>, cage listens on <path> and adds a touchscreen to the seat.
 * Each line on the socket is one touch event, coordinates 0..1 across the output:
 *   d <id> <x> <y>   finger down        m <id> <x> <y>   finger moved
 *   u <id>           finger up          c <id>           finger cancelled
 * so the browser's multi-finger touches reach the client as ordinary wl_touch events.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <wlr/interfaces/wlr_touch.h>

struct rm_vtouch {
	struct wlr_touch touch;
	struct cg_seat *seat;
	struct wl_event_source *listen_source;
	struct wl_event_source *conn_source;
	int conn_fd;
	size_t len;
	char buf[8192];
};

static const struct wlr_touch_impl rm_vtouch_impl = {
	.name = "robloxmodal-virtual-touch",
};

static uint32_t
rm_vtouch_now(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static void
rm_vtouch_line(struct rm_vtouch *vt, const char *line)
{
	char verb = 0;
	int id = 0;
	double x = 0, y = 0;
	int n = sscanf(line, " %c %d %lf %lf", &verb, &id, &x, &y);
	if (n < 2) {
		return;
	}
	if (x < 0) x = 0;
	if (x > 1) x = 1;
	if (y < 0) y = 0;
	if (y > 1) y = 1;
	uint32_t t = rm_vtouch_now();
	if (verb == 'd' && n == 4) {
		struct wlr_touch_down_event ev = {.touch = &vt->touch, .time_msec = t, .touch_id = id, .x = x, .y = y};
		wl_signal_emit_mutable(&vt->touch.events.down, &ev);
	} else if (verb == 'm' && n == 4) {
		struct wlr_touch_motion_event ev = {.touch = &vt->touch, .time_msec = t, .touch_id = id, .x = x, .y = y};
		wl_signal_emit_mutable(&vt->touch.events.motion, &ev);
	} else if (verb == 'u') {
		struct wlr_touch_up_event ev = {.touch = &vt->touch, .time_msec = t, .touch_id = id};
		wl_signal_emit_mutable(&vt->touch.events.up, &ev);
	} else if (verb == 'c') {
		struct wlr_touch_cancel_event ev = {.touch = &vt->touch, .time_msec = t, .touch_id = id};
		wl_signal_emit_mutable(&vt->touch.events.cancel, &ev);
	} else {
		return;
	}
	wl_signal_emit_mutable(&vt->touch.events.frame, NULL);
}

static void
rm_vtouch_close(struct rm_vtouch *vt)
{
	if (vt->conn_source) {
		wl_event_source_remove(vt->conn_source);
		vt->conn_source = NULL;
	}
	if (vt->conn_fd >= 0) {
		close(vt->conn_fd);
		vt->conn_fd = -1;
	}
	vt->len = 0;
}

static int
rm_vtouch_readable(int fd, uint32_t mask, void *data)
{
	struct rm_vtouch *vt = data;
	ssize_t n = read(fd, vt->buf + vt->len, sizeof(vt->buf) - 1 - vt->len);
	if (n < 0 && (errno == EAGAIN || errno == EINTR)) {
		return 0;
	}
	if (n <= 0) {
		rm_vtouch_close(vt);
		return 0;
	}
	vt->len += (size_t)n;
	vt->buf[vt->len] = '\0';
	char *start = vt->buf;
	char *nl;
	while ((nl = strchr(start, '\n'))) {
		*nl = '\0';
		rm_vtouch_line(vt, start);
		start = nl + 1;
	}
	vt->len -= (size_t)(start - vt->buf);
	memmove(vt->buf, start, vt->len);
	if (vt->len >= sizeof(vt->buf) - 1) {
		vt->len = 0; /* a line that long is not one of ours */
	}
	return 0;
}

static int
rm_vtouch_accept(int fd, uint32_t mask, void *data)
{
	struct rm_vtouch *vt = data;
	int conn = accept(fd, NULL, NULL);
	if (conn < 0) {
		return 0;
	}
	fcntl(conn, F_SETFD, FD_CLOEXEC);
	fcntl(conn, F_SETFL, fcntl(conn, F_GETFL) | O_NONBLOCK);
	rm_vtouch_close(vt); /* the newest connection wins */
	vt->conn_fd = conn;
	vt->conn_source = wl_event_loop_add_fd(wl_display_get_event_loop(vt->seat->server->wl_display), conn,
					       WL_EVENT_READABLE, rm_vtouch_readable, vt);
	return 0;
}

static void
rm_virtual_touch(struct cg_seat *seat)
{
	const char *path = getenv("CAGE_VIRTUAL_TOUCH");
	if (!path || !*path) {
		return;
	}
	struct sockaddr_un addr = {.sun_family = AF_UNIX};
	if (strlen(path) >= sizeof(addr.sun_path)) {
		wlr_log(WLR_ERROR, "CAGE_VIRTUAL_TOUCH path is too long");
		return;
	}
	strcpy(addr.sun_path, path);
	int fd = socket(AF_UNIX, SOCK_STREAM, 0);
	unlink(path);
	if (fd < 0 || bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0 || listen(fd, 4) < 0) {
		wlr_log_errno(WLR_ERROR, "Cannot listen on %s", path);
		if (fd >= 0) {
			close(fd);
		}
		return;
	}
	fcntl(fd, F_SETFD, FD_CLOEXEC);
	fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
	struct rm_vtouch *vt = calloc(1, sizeof(*vt));
	if (!vt) {
		close(fd);
		return;
	}
	vt->seat = seat;
	vt->conn_fd = -1;
	wlr_touch_init(&vt->touch, &rm_vtouch_impl, "robloxmodal-virtual-touch");
	vt->listen_source = wl_event_loop_add_fd(wl_display_get_event_loop(seat->server->wl_display), fd,
						 WL_EVENT_READABLE, rm_vtouch_accept, vt);
	handle_new_touch(seat, &vt->touch);
	update_capabilities(seat);
	wlr_log(WLR_INFO, "Virtual touchscreen listening on %s", path);
}
/* ---- end robloxmodal ------------------------------------------------------------------- */

'''


# Added to cage's output.c where a new output is enabled: the headless backend's only output is
# 1280x720, and changing it afterwards with wlr-randr trips an assertion in this cage revision.
CAGE_SIZE_C = r'''
	/* robloxmodal: CAGE_OUTPUT_SIZE=<w>x<h> sets the size of the output. */
	const char *rm_size = getenv("CAGE_OUTPUT_SIZE");
	int rm_w = 0, rm_h = 0;
	if (rm_size && sscanf(rm_size, "%dx%d", &rm_w, &rm_h) == 2 && rm_w > 0 && rm_h > 0) {
		wlr_output_state_set_custom_mode(&state, rm_w, rm_h, 0);
	}

'''


def _cage_patcher() -> str:
    """A script, run in cage's source tree during the image build, that applies the edits below."""
    edits = [
        # (file, anchor, text, where: True after the anchor, False before it, None instead of it)
        ("seat.c", "struct cg_seat *\nseat_create(", CAGE_TOUCH_C, False),
        ("seat.c", "\twl_signal_add(&backend->events.new_input, &seat->new_input);\n",
         "\n\trm_virtual_touch(seat);\n", True),
        # CAGE_HIDE_CURSOR: no pointer arrow drawn on the screen (mobile mode has no mouse)
        ("seat.c", "\tif ((caps & WL_SEAT_CAPABILITY_POINTER) == 0) {\n\t\twlr_cursor_unset_image",
         "\tif ((caps & WL_SEAT_CAPABILITY_POINTER) == 0 || getenv(\"CAGE_HIDE_CURSOR\")) {\n"
         "\t\twlr_cursor_unset_image", None),
        ("seat.c", "\tif (focused_client == event->seat_client->client) {\n\t\twlr_cursor_set_surface",
         "\tif (focused_client == event->seat_client->client && !getenv(\"CAGE_HIDE_CURSOR\")) {\n"
         "\t\twlr_cursor_set_surface", None),
        ("output.c", "#include <stdlib.h>\n", "#include <stdio.h>\n", True),
        ("output.c", "\tif (server->output_mode == CAGE_MULTI_OUTPUT_MODE_LAST && wl_list_length(&server->outputs) > 1) {\n",
         CAGE_SIZE_C, False),
    ]
    return (
        f"EDITS = {edits!r}\n"
        "for name, anchor, text, after in EDITS:\n"
        "    src = open(name).read()\n"
        "    assert src.count(anchor) == 1, f'unexpected cage revision: {name}'\n"
        "    src = src.replace(anchor, text if after is None else anchor + text if after else text + anchor)\n"
        "    open(name, 'w').write(src)\n"
    )


image = (
    modal.Image.from_registry("ubuntu:24.04", add_python="3.12")
    .apt_install(
        "curl", "ca-certificates", "git", "build-essential", "pkg-config", "libssl-dev",
        "cage", "grim", "wayvnc", "libgl1-mesa-dri", "mesa-vulkan-drivers",
    )
    # Cordial's own Roblox downloader, which refuses any build not signed by Roblox. Built and the
    # Rust toolchain removed in one layer, so the image keeps only the binary.
    .run_commands(
        "curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal"
        f" && git clone -q --depth 1 --branch v{CORDIAL_VERSION} https://github.com/luohoa97/cordial /tmp/cordial"
        " && . $HOME/.cargo/env && cd /tmp/cordial"
        " && cargo build --release -p cordial-update --example fetch_probe"
        " && cp target/release/examples/fetch_probe /usr/local/bin/"
        " && cd / && rm -rf /tmp/cordial $HOME/.cargo $HOME/.rustup",
    )
    .run_commands(
        f"curl -sSL -o /tmp/cordial.deb {CORDIAL_DEB}"
        " && apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q /tmp/cordial.deb"
        " && rm -rf /tmp/cordial.deb /var/lib/apt/lists/*",
    )
    # cage, rebuilt with a touchscreen whose fingers arrive over a socket (CAGE_TOUCH_C above):
    # neither VNC nor any Wayland protocol can deliver touch, and Roblox only shows its
    # thumbstick and jump button to a touchscreen. Pinned to the revision Ubuntu 24.04 packages.
    .apt_install(
        "meson", "ninja-build", "libwlroots-dev", "libwayland-dev", "wayland-protocols",
        "libxkbcommon-dev", "libpixman-1-dev",
    )
    .run_commands(
        f"echo {base64.b64encode(_cage_patcher().encode()).decode()} | base64 -d > /tmp/patch_cage.py"
        " && git clone -q https://github.com/cage-kiosk/cage /tmp/cage && cd /tmp/cage"
        f" && git checkout -q {CAGE_COMMIT} && python3 /tmp/patch_cage.py"
        " && meson setup build -Dman-pages=disabled --buildtype=release && ninja -C build"
        " && install -m 755 build/cage /usr/local/bin/cage-touch && cd / && rm -rf /tmp/cage /tmp/patch_cage.py",
    )
    .uv_pip_install("aiohttp==3.12.15")
)

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
registry = modal.Dict.from_name(REGISTRY_NAME, create_if_missing=True)

PORT = 6080


@app.function(
    image=image,
    volumes={"/vol": volume},
    cpu=8,  # the relay overrides cpu, memory and timeout per launch with .with_options()
    memory=8192,
    timeout=3 * 3600,
    scaledown_window=2,  # stop billing as soon as the session ends
)
def session(sid: str, key: str, mode: str, idle_minutes: float, width: int, height: int) -> str:
    import asyncio

    with modal.forward(PORT) as tunnel:
        entry = registry.get(sid) or {}
        registry[sid] = {**entry, "url": tunnel.url, "status": "running"}
        try:
            server = Session(key, mode, idle_minutes, width, height)
            why = asyncio.run(server.serve())
        finally:
            registry.pop(sid, None)
    return why


# ---------------------------------------------------------------------------------------------
# Everything below runs inside the session container only.

VOL = "/vol"
APK_DIR = f"{VOL}/roblox"
LIB_ROOT = "/root/roblox"
LIB_DIR = f"{LIB_ROOT}/lib/x86_64"
XDG = "/tmp/xdg"
TOUCH_SOCKET = f"{XDG}/touch.sock"  # the patched cage's touchscreen
GO = f"{XDG}/go"  # cage's child waits for this before starting Roblox
LOG_FILE = "/tmp/session.log"
VNC_PORT = 5900


class Session:
    def __init__(self, key: str, mode: str, idle_minutes: float, width: int, height: int):
        from collections import deque

        self.key = key
        self.mode = "mobile" if mode == "mobile" else "pc"
        self.idle_seconds = max(60, int(idle_minutes * 60))
        self.width, self.height = width, height
        self.state = {"phase": "starting", "message": "Starting the session", "viewers": 0}
        self.started_at = time.time()
        self.last_viewer_at = time.time()
        self.log = deque(maxlen=300)
        self.procs: dict = {}
        self.anchor = None
        self.touch_conn = None
        self.run = 0  # which launch of Roblox this is; Restart starts a new one
        self.unsticks = 0  # automatic restarts after Roblox froze while loading

    # -------------------------------------------------------------------------------- Roblox

    def set_phase(self, phase: str, message: str) -> None:
        self.state.update(phase=phase, message=message)
        self.note(f"[session] {phase}: {message}")

    def note(self, line: str) -> None:
        self.log.append(line)
        print(line, flush=True)

    async def pump(self, proc) -> None:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line.startswith(("[stub]", "> ", "Errors from xkbcomp")) or not line.strip():
                continue
            with open(LOG_FILE, "a") as f:  # everything, for `modal container exec ... cat`
                f.write(line + "\n")
            if self.state["phase"] == "downloading" and line.endswith(" bytes"):
                done, _, total = line.rsplit(" ", 2)[-2].partition("/")
                if done.isdigit() and total.isdigit() and int(total):
                    pct = 100 * int(done) // int(total)
                    self.state["message"] = f"Downloading Roblox, first session only: {pct}%"
                continue
            self.log.append(line)
            if "app ready:" in line and self.state["phase"] == "loading":
                # Roblox reports its routing apps before anything is on screen; the first real
                # screen (Landing, or Home when signed in) draws a few seconds after it reports.
                screen = line.rsplit("app ready:", 1)[1].strip()
                if screen not in ("PlatformAccountRouter", "Startup"):
                    self.ready_later(3)

    def ready_later(self, seconds: float) -> None:
        import asyncio

        run = self.run

        def mark() -> None:
            if run == self.run and self.state["phase"] == "loading":
                self.set_phase("ready", "Roblox is running")

        asyncio.get_running_loop().call_later(seconds, mark)

    def unstick_later(self, seconds: float) -> None:
        """Roblox sometimes freezes on its loading screen (Cordial's launcher restarts it too).

        Normally it shows its first screen 10-25 seconds after starting. If it hasn't after
        `seconds`, start it again, twice at most; after that, show whatever is on screen.
        """
        import asyncio

        run = self.run

        async def check() -> None:
            await asyncio.sleep(seconds)
            if run != self.run or self.state["phase"] != "loading":
                return
            if self.unsticks >= 2:
                self.set_phase("ready", "Roblox is running (it may still be loading)")
                return
            self.unsticks += 1
            self.set_phase("restarting", f"Roblox froze while loading; restarting it ({self.unsticks} of 2)")
            await self.stop_roblox()
            await self.start_roblox()

        asyncio.get_running_loop().create_task(check())

    async def ensure_build(self) -> str:
        """The Roblox Android build, downloaded into the Volume on first use."""
        import asyncio
        import glob
        import shutil

        have = sorted(glob.glob(f"{APK_DIR}/*.apk"))
        if have:
            return have[0]
        self.set_phase("downloading", "Downloading Roblox (about 230 MB, first session only)")
        tmp = f"{VOL}/roblox.partial.{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        proc = await asyncio.create_subprocess_exec(
            "fetch_probe", tmp, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await self.pump(proc)
        code = await proc.wait()
        apks = sorted(glob.glob(f"{tmp}/*.apk"))
        if code != 0 or not apks:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"downloading Roblox failed (exit {code})")
        if glob.glob(f"{APK_DIR}/*.apk"):  # another session finished first
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            shutil.rmtree(APK_DIR, ignore_errors=True)
            os.rename(tmp, APK_DIR)
        return sorted(glob.glob(f"{APK_DIR}/*.apk"))[0]

    def cordial_env(self) -> dict:
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR=XDG,
            # cage on wlroots' headless backend: no screen, no input devices of its own
            WLR_BACKENDS="headless",
            WLR_LIBINPUT_NO_DEVICES="1",
            CAGE_OUTPUT_SIZE=f"{self.width}x{self.height}",
            CORDIAL_GRAPHICS="gles",  # no GPU: Mesa's llvmpipe draws on the CPU
            CORDIAL_RESOLUTION=f"{self.width}x{self.height}",
            # No Cordial title bar: the game fills the screen and there is no close button to hit.
            CORDIAL_TITLE_BAR="hidden",
        )
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        if self.mode == "mobile":
            # A touchscreen on the seat, and Roblox told it runs on an Android tablet: it then
            # builds its touch interface (thumbstick, jump button) instead of the keyboard one.
            env["CAGE_VIRTUAL_TOUCH"] = TOUCH_SOCKET
            env["CAGE_HIDE_CURSOR"] = "1"
            env["CORDIAL_DEVICE_PROFILE"] = "android-tablet"
            env["CORDIAL_INPUT_TOUCH"] = "1"
        else:
            # Keyboard and mouse: WASD to move, space to jump, right-drag to look.
            env["CORDIAL_INPUT_TOUCH"] = "0"
        return env

    async def start_roblox(self) -> None:
        """cage, then wayvnc and an input anchor, and only then Roblox.

        Cordial binds the seat's keyboard and mouse once, when its window opens; it picks up a
        touchscreen later but not those. wayvnc only creates its virtual keyboard and mouse for
        a connected VNC client. So a VNC client of our own (the anchor) must be connected before
        Roblox starts, and stays connected for as long as Roblox runs.
        """
        import asyncio
        import glob
        import shlex
        import zipfile

        try:
            apk = await self.ensure_build()
            self.set_phase("launching", "Starting Roblox")
            if not os.path.exists(f"{LIB_DIR}/libroblox.so"):
                with zipfile.ZipFile(apk) as z:
                    for name in z.namelist():
                        if name.startswith("lib/x86_64/"):
                            z.extract(name, LIB_ROOT)
            os.makedirs(XDG, mode=0o700, exist_ok=True)
            for old in glob.glob(f"{XDG}/wayland-*") + [GO]:
                if os.path.exists(old):
                    os.remove(old)

            # --host-libc is what Cordial's own launcher passes; without it libc data imports
            # such as `environ` resolve to stubs and the engine crashes while loading.
            # --run 0: no timer; the session decides when Roblox stops.
            roblox = ["cordial-run", "--lib-dir", LIB_DIR, "--apk", apk,
                      "--host-libc", "--game-activity", "--run", "0"]
            child = f"while [ ! -e {GO} ]; do sleep 0.1; done; exec {shlex.join(roblox)}"
            proc = await asyncio.create_subprocess_exec(
                "cage-touch", "--", "sh", "-c", child,
                env=self.cordial_env(), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
            self.procs["cage"] = proc
            asyncio.create_task(self.pump(proc))
            display = await self.wait_for_display(proc)
            await self.start_vnc(display)
            await self.connect_anchor()
            open(GO, "w").close()
            self.run += 1
            self.set_phase("loading", "Roblox is loading")
            self.unstick_later(75)
            asyncio.create_task(self.watch_exit(proc))
        except Exception as e:  # shown on the site; the session stays up so it can be read
            self.set_phase("exited", f"Could not start Roblox: {e}")

    async def wait_for_display(self, proc) -> str:
        import asyncio
        import glob

        for _ in range(600):
            sockets = [s for s in glob.glob(f"{XDG}/wayland-*") if not s.endswith(".lock")]
            if sockets:
                return os.path.basename(sockets[0])
            if proc.returncode is not None:
                raise RuntimeError("the compositor exited before its display came up")
            await asyncio.sleep(0.2)
        raise RuntimeError("the display never came up")

    async def start_vnc(self, display: str) -> None:
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            "wayvnc", "--max-fps=30", "127.0.0.1", str(VNC_PORT),
            env=dict(os.environ, XDG_RUNTIME_DIR=XDG, WAYLAND_DISPLAY=display),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.procs["wayvnc"] = proc
        asyncio.create_task(self.pump(proc))
        for _ in range(100):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
                writer.close()
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise RuntimeError("the VNC server did not start")

    async def connect_anchor(self) -> None:
        """A VNC client that never asks for pictures, so the seat keeps a keyboard and mouse."""
        import asyncio

        reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
        await reader.readexactly(12)  # server version
        writer.write(b"RFB 003.008\n")
        count = (await reader.readexactly(1))[0]
        if 1 not in await reader.readexactly(count):
            raise RuntimeError("the VNC server wants a password")
        writer.write(b"\x01")  # security type None
        if await reader.readexactly(4) != b"\0\0\0\0":
            raise RuntimeError("the VNC server refused the connection")
        writer.write(b"\x01")  # shared: other viewers may connect alongside
        init = await reader.readexactly(24)
        await reader.readexactly(int.from_bytes(init[20:24], "big"))  # desktop name

        async def drain():  # bells and clipboard updates; nothing else arrives unasked
            while await reader.read(4096):
                pass

        asyncio.create_task(drain())
        self.anchor = writer
        await asyncio.sleep(0.3)  # let wayvnc create the virtual devices

    async def watch_exit(self, proc) -> None:
        code = await proc.wait()
        if self.state["phase"] not in ("stopping", "restarting"):
            self.set_phase("exited", f"Roblox exited (code {code}). Press Restart to start it again.")
            await self.stop_roblox()

    async def stop_roblox(self) -> None:
        if self.anchor:
            self.anchor.close()
            self.anchor = None
        self.touch_conn = None
        await self.stop_process("cage")  # and Roblox with it: same process group
        await self.stop_process("wayvnc")

    async def stop_process(self, name: str) -> None:
        import asyncio
        import signal

        proc = self.procs.pop(name, None)
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), 15)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # -------------------------------------------------------------------------------- touch

    async def touch(self, line: str) -> None:
        """One line to the patched cage's touchscreen (see CAGE_TOUCH_C)."""
        import asyncio

        for attempt in range(2):
            try:
                if self.touch_conn is None:
                    _, self.touch_conn = await asyncio.open_unix_connection(TOUCH_SOCKET)
                self.touch_conn.write((line + "\n").encode())
                await self.touch_conn.drain()
                return
            except OSError:
                self.touch_conn = None
                if attempt:
                    raise

    # -------------------------------------------------------------------------------- HTTP

    async def serve(self) -> str:
        import asyncio
        import hmac

        from aiohttp import web

        @web.middleware
        async def guard(request, handler):
            if request.method == "OPTIONS":
                resp = web.Response()
            elif not hmac.compare_digest(request.query.get("k", ""), self.key):
                self.note(f"[session] refused {request.path}: wrong session key")
                resp = web.json_response({"error": "wrong session key"}, status=403)
            else:
                resp = await handler(request)
            if not isinstance(resp, web.WebSocketResponse):
                resp.headers["Access-Control-Allow-Origin"] = "*"
                resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                resp.headers["Cache-Control"] = "no-store"
            return resp

        self.done = asyncio.Event()
        self.end_reason = ""
        web_app = web.Application(middlewares=[guard])
        web_app.router.add_get("/status", self.http_status)
        web_app.router.add_get("/vnc", self.ws_vnc)
        web_app.router.add_get("/touch", self.ws_touch)
        web_app.router.add_post("/restart", self.http_restart)
        web_app.router.add_post("/shutdown", self.http_shutdown)
        web_app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
        runner = web.AppRunner(web_app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        self.note(f"[session] serving, mode={self.mode}, {self.width}x{self.height}")

        asyncio.create_task(self.idle_watchdog())
        asyncio.create_task(self.start_roblox())
        await self.done.wait()
        await self.stop_roblox()
        await runner.cleanup()
        return self.end_reason

    def end(self, why: str) -> None:
        if not self.done.is_set():
            self.set_phase("stopping", f"Session ended: {why}")
            self.end_reason = why
            self.done.set()

    async def idle_watchdog(self) -> None:
        import asyncio

        while not self.done.is_set():
            await asyncio.sleep(10)
            idle = time.time() - self.last_viewer_at
            if self.state["viewers"] == 0 and idle > self.idle_seconds:
                self.end(f"nobody was watching for {self.idle_seconds // 60} minutes")

    async def http_status(self, request):
        from aiohttp import web

        try:
            lines = min(300, max(1, int(request.query.get("lines", "25"))))
        except ValueError:
            lines = 25
        return web.json_response({
            **self.state,
            "mode": self.mode,
            "width": self.width,
            "height": self.height,
            "uptime": round(time.time() - self.started_at),
            "idle_minutes": self.idle_seconds // 60,
            "log": list(self.log)[-lines:],
        })

    async def ws_vnc(self, request):
        import asyncio

        from aiohttp import WSMsgType, web

        ws = web.WebSocketResponse(protocols=("binary",), max_msg_size=0, heartbeat=20)
        await ws.prepare(request)
        browser = request.headers.get("User-Agent", "?")[:120]
        try:
            if "wayvnc" not in self.procs:
                raise OSError("not ready")
            reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
        except OSError:
            self.note(f"[session] viewer turned away, Roblox not up yet ({browser})")
            await ws.close(code=4000, message=b"not ready")
            return ws
        self.state["viewers"] += 1
        began = time.time()
        sent = received = 0
        self.note(f"[session] viewer connected ({browser})")

        async def downstream():
            nonlocal sent
            while data := await reader.read(65536):
                await ws.send_bytes(data)
                sent += len(data)
            await ws.close()

        task = asyncio.create_task(downstream())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    received += len(msg.data)
                    writer.write(msg.data)
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            task.cancel()
            writer.close()
            self.state["viewers"] -= 1
            self.last_viewer_at = time.time()
            self.note(f"[session] viewer left after {time.time() - began:.0f} s: {sent} bytes of picture "
                      f"sent, {received} bytes received, close code {ws.close_code}")
        return ws

    async def ws_touch(self, request):
        """Mobile mode: `d|m|u <finger> <x> <y>`, screen pixels, any number of fingers."""
        from aiohttp import WSMsgType, web

        ids: dict[str, int] = {}  # the browser's pointer ids -> small touch ids
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.note("[session] touch channel connected")
        touches = 0
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                parts = msg.data.split()
                if len(parts) != 4 or parts[0] not in ("d", "m", "u"):
                    continue
                verb, finger = parts[0], parts[1]
                try:
                    x = min(max(float(parts[2]) / self.width, 0.0), 1.0)
                    y = min(max(float(parts[3]) / self.height, 0.0), 1.0)
                except ValueError:
                    continue
                if verb == "d":
                    if finger in ids or len(ids) >= 10:
                        continue
                    ids[finger] = min(set(range(10)) - set(ids.values()))
                elif finger not in ids:
                    continue
                tid = ids.pop(finger) if verb == "u" else ids[finger]
                touches += verb == "d"
                try:
                    await self.touch(f"{verb} {tid} {x:.5f} {y:.5f}" if verb != "u" else f"u {tid}")
                except OSError:
                    pass  # Roblox is not up yet, or restarting
        finally:
            for tid in ids.values():  # the page went away mid-touch: lift those fingers
                try:
                    await self.touch(f"u {tid}")
                except OSError:
                    pass
            self.note(f"[session] touch channel closed after {touches} touches")
        return ws

    async def http_restart(self, request):
        import asyncio

        from aiohttp import web

        if self.state["phase"] in ("starting", "downloading", "launching", "restarting", "stopping"):
            return web.json_response({"ok": False, "phase": self.state["phase"]})
        self.set_phase("restarting", "Restarting Roblox")
        self.unsticks = 0
        await self.stop_roblox()
        asyncio.create_task(self.start_roblox())
        return web.json_response({"ok": True})

    async def http_shutdown(self, request):
        from aiohttp import web

        self.end("stopped from the site")
        return web.json_response({"ok": True})
