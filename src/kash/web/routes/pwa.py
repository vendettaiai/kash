"""Install Kash as an app (PWA): the web app manifest and a small service worker.

Both are public (the browser fetches them without the login cookie) and contain
nothing about the user. The service worker only caches files under /static/
(their URLs change with their contents) and shows a short "you're offline" page
when a page can't load. Pages and data always come from the server, never a cache.
It also shows push notifications, which arrive encrypted (services/notifications).
"""

import hashlib
import json

from fastapi import APIRouter
from fastapi.responses import Response

from kash.web.static_files import static_url

router = APIRouter()

THEME_COLOR = "#059669"  # the icon's green
BACKGROUND_COLOR = "#f8fafc"  # the app's light background (splash screen)


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> Response:
    """What phones and browsers use to install Kash: name, icons, colors."""
    data = {
        "name": "Kash",
        "short_name": "Kash",
        "description": "Personal finance and budgeting",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": BACKGROUND_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": static_url("icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static_url("icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {
                "src": static_url("icons/icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return Response(
        json.dumps(data),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


OFFLINE_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kash - Offline</title>
<style>body{font-family:system-ui,sans-serif;background:#f8fafc;color:#0f172a;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center;padding:1rem}
button{margin-top:1rem;padding:.6rem 1.2rem;border:0;border-radius:.75rem;background:#059669;
color:#fff;font-weight:600;font-size:1rem}
@media (prefers-color-scheme:dark){body{background:#000000;color:#f8fafc}}</style></head>
<body><div><h1>You're offline</h1><p>Kash needs a connection to your server.</p>
<button onclick="location.reload()">Try again</button></div></body></html>"""

# Caches /static/ files only; pages always come from the server.
SERVICE_WORKER = """// Kash service worker
const CACHE = "kash-static-__VERSION__";
const OFFLINE = __OFFLINE__;
const ICON = __ICON__;
const BADGE = __BADGE__;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(
        () => new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })
      )
    );
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(request).then(
          (hit) =>
            hit ||
            fetch(request).then((response) => {
              if (response.ok) cache.put(request, response.clone());
              return response;
            })
        )
      )
    );
  }
});

// Browsers require every push to show something, so a message that can't be read still does.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data.json();
  } catch (error) {}
  event.waitUntil(
    self.registration.showNotification(data.title || "Kash", {
      body: data.body || "",
      icon: ICON,
      badge: BADGE,
      tag: data.tag || undefined,
      data: { url: data.url },
    })
  );
});

// A tap opens the page it's about, in the app if it's already running.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data.url || "/", self.location.origin);
  const path = url.origin === self.location.origin ? url.pathname + url.search : "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (windows) => {
      for (const client of windows) {
        try {
          await client.focus();
          await client.navigate(path);
          return;
        } catch (error) {}
      }
      await self.clients.openWindow(path);
    })
  );
});
"""


def _service_worker() -> str:
    # A new version (and so a fresh cache) whenever the app's CSS/JS changes
    assets = "".join(
        static_url(p)
        for p in ("css/tailwind.min.css", "css/app.css", "js/htmx.min.js", "js/kash.js")
    )
    version = hashlib.sha256(assets.encode()).hexdigest()[:8]
    return (
        SERVICE_WORKER.replace("__VERSION__", version)
        .replace("__OFFLINE__", json.dumps(OFFLINE_PAGE))
        .replace("__ICON__", json.dumps(static_url("icons/icon-192.png")))
        .replace("__BADGE__", json.dumps(static_url("icons/badge-96.png")))
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    """Served from the site root so it covers every page."""
    return Response(
        _service_worker(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )
