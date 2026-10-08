# Roblox on Modal

A GitHub Pages site that runs Roblox in a [Modal](https://modal.com) container and streams it to
your browser. Roblox's official Android client runs through
[Cordial](https://github.com/luohoa97/cordial) on CPU cores in **your own** Modal workspace, so
whoever enters a Modal token pays for the time their session runs.

- **Settings**: CPU cores, PC or Mobile controls, your Modal token ID and secret.
- **Load Roblox**: starts a container and shows Roblox in the page. Sign in and play.
- **Stop**: shuts the container down so billing stops.

## Set it up (once)

1. **Deploy the relay.** A web page cannot call Modal's API itself (it is gRPC with no CORS), so a
   small relay does it with the visitor's token. It scales to zero and costs its owner cents a
   month.

   ```bash
   pip install modal
   modal setup                   # log in to the Modal account that hosts the relay
   cd backend
   modal deploy relay.py
   ```

   It prints `https://<workspace>--roblox-relay-web.modal.run`. If that is not the address in
   [`assets/config.js`](assets/config.js), put yours there (visitors can also override it in
   Settings → Advanced).

2. **Turn on GitHub Pages.** Repository Settings → Pages → Build and deployment → Deploy from a
   branch → pick the branch with these files and `/ (root)` → Save. The site appears at
   `https://<user>.github.io/RobloxModal/`.

3. **Open the site, then Settings.** Create a token at
   [modal.com/settings/tokens](https://modal.com/settings/tokens), paste the ID (`ak-...`) and the
   secret (`as-...`), pick cores and controls, and Save.

The **first** Load Roblox in a Modal workspace builds the container image there (about 5-10
minutes) and downloads Roblox's Android build (about 230 MB) into a Volume named
`roblox-modal-data`. Later launches reach the Roblox start screen in under a minute.

## Controls

| | PC | Mobile |
|---|---|---|
| Roblox thinks it is on | a Windows PC with keyboard and mouse | an Android tablet with a touchscreen |
| Move | W A S D | Roblox's own thumbstick |
| Jump | Space | Roblox's jump button |
| Camera | right-drag, arrow keys | drag on the right half of the screen |
| Typing | your keyboard | **☰ → Keyboard** opens the phone's keyboard |

Touches are real multi-finger touches, so you can move, jump and turn at the same time.

The **☰** button in the corner while playing has: Keyboard, Fullscreen (on Android it also locks
landscape), Restart (Roblox only, not the container), Menu (back to the site, session keeps
running), Stop.

## What it costs

Sessions bill your Modal workspace for CPU and memory while they run (Modal list prices, October
2026; `modal billing rates` shows yours). The relay asks for 4 GiB plus 0.5 GiB per core.

| Cores | Per hour |
|---|---|
| 2 | about $0.13 |
| 4 | about $0.24 |
| 8 | about $0.44 |
| 16 | about $0.85 |

Plus about $0.04 per GB of video streamed. More cores give a smoother picture: Roblox draws on the
CPU (Mesa llvmpipe) because Modal's GPU containers can't present graphics, which is why this uses
no GPU at all.

**Nothing keeps running by accident:**

- **Stop** ends the container, and also stops any other session this site started on your account.
- With nobody watching (tab closed, phone locked), a session stops itself after 10 minutes
  (Settings → Advanced).
- Every session ends after 3 hours at most (Settings → Advanced, up to 24).
- To check by hand: [modal.com/apps](https://modal.com/apps) → `roblox-modal`, or
  `modal app stop roblox-modal`.

## How it works

```
GitHub Pages site (this repo)                    noVNC in the page
   │  your token, over HTTPS                       ▲ screen    │ keys, mouse, touches
   ▼                                               │           ▼
relay (backend/relay.py, the site owner's Modal)   │  wss://...modal.host  (modal.forward)
   │  deploys backend/session_app.py into YOUR     │
   │  workspace and calls it                       │
   ▼                                               │
session container (YOUR Modal workspace, CPU only) ┘
   cage            headless Wayland compositor, patched with a touchscreen fed from a socket
     └ cordial-run  Roblox's Android client, run natively by Cordial
   wayvnc          VNC server on the compositor
   aiohttp server  /vnc (picture + keyboard/mouse), /touch, /status, /restart, /shutdown
```

- Every request to a session needs that session's random key, which only the browser that
  started it (or anyone holding your Modal token) can get.
- Mobile mode: the page turns your touches into touch events; the patched cage presents them to
  Cordial as a real touchscreen, and Cordial tells Roblox it is on an Android tablet, so Roblox
  shows its touch controls instead of the keyboard-and-mouse ones.
- PC mode: keyboard and mouse go through VNC unchanged; Roblox handles WASD itself.

| File | What |
|---|---|
| `index.html`, `assets/app.js`, `assets/style.css` | the site |
| `assets/config.js` | the relay address the site uses by default |
| `assets/vendor/novnc/` | noVNC 1.7.0 (MPL-2.0), the VNC client |
| `backend/relay.py` | the relay; deploy once |
| `backend/session_app.py` | the image and the session server; the relay deploys it for each user |

## Your token

The token is saved in your browser's local storage and sent to the relay only to start, list and
stop sessions; the relay does not log or store it. It does pass through code run by whoever
deployed the relay, so only enter it on a site whose relay you trust, or deploy your own. A token
can do anything on its Modal workspace: consider a separate workspace for this, and revoke the
token at [modal.com/settings/tokens](https://modal.com/settings/tokens) whenever you like.
Settings → Advanced → Forget saved settings removes it from the browser.

## Known limits

- No sound.
- The picture is CPU-drawn: playable on menus and light games, slow in heavy ones.
- Tested on Roblox's own menus and sign-in screen (taps, typing, a two-finger drag). Joining a
  game needs a signed-in account, so the in-game thumbstick and jump button were not tried.
- Sign-in with a real account was not tested here. If Roblox shows a challenge that does not
  work, use **Quick Sign-in** on the login screen (approve it from another device).
- Cordial is experimental and unofficial: use a Roblox account you don't mind risking. Roblox can
  crash now and then; press Restart.
- PC mode: if W A S D do nothing right after joining a game, move the mouse while the game loads
  (a Cordial bug, [#29](https://github.com/luohoa97/cordial/issues/29)).
- Mouse look that needs a locked pointer (shift-lock, first person) doesn't work through VNC; use
  right-drag.

## Credits

[Cordial](https://github.com/luohoa97/cordial) (GPL-3.0) runs the Android client; it is downloaded
from its releases page when the image is built, not included here.
[cage](https://github.com/cage-kiosk/cage) (MIT) is rebuilt with a small patch when the image is
built. [wayvnc](https://github.com/any1/wayvnc) (ISC) serves the screen.
[noVNC](https://github.com/novnc/noVNC) (MPL-2.0) shows it in the page. Not affiliated with
Roblox or Modal.
