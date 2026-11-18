// Kash in the browser: moving between pages without full reloads.
//
// <body hx-boost="true"> (base.html) turns links and forms into background requests that
// swap the page body, so the CSS and scripts stay loaded. This file adds what makes that
// feel finished:
// - a save that comes back to the same page updates it in place: no jump to the top,
//   open sections stay open, no extra Back step;
// - Back and Forward always show fresh data and return to where you were scrolled;
// - notices (?notice=...) show once and are not carried into later links;
// - a form can't be sent twice while it's working;
// - failed requests say so instead of doing nothing.
// The server stays the source of truth: nothing here keeps data, only scroll positions.
(function () {
  "use strict";

  const FLASH = ["notice", "error", "synced"];
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // A page's address without one-time notices.
  function pageKey(url) {
    const u = new URL(url, location.href);
    FLASH.forEach((key) => u.searchParams.delete(key));
    return u.pathname + u.search;
  }

  // Navigate like a link would (used where a select or a script picks the address).
  window.kashGo = function (url) {
    return htmx.ajax("GET", url, { target: "body", swap: "innerHTML show:top", push: "true" });
  };

  // Reload this page's content in place (after a change made outside a form).
  window.kashRefresh = function () {
    return htmx.ajax("GET", location.pathname + location.search, {
      target: "body",
      swap: "innerHTML show:none transition:false",
      headers: { "X-Kash-Stay": "1" },
    });
  };

  // Change the address without a new history entry (e.g. the open tab on Rules).
  window.kashReplaceUrl = function (url) {
    history.replaceState(history.state, "", url);
    current = pageKey(location.href);
  };

  // --- Notices show once ---------------------------------------------------------------

  function dropFlashFromUrl() {
    const url = new URL(location.href);
    if (!FLASH.some((key) => url.searchParams.has(key))) return;
    FLASH.forEach((key) => url.searchParams.delete(key));
    window.kashReplaceUrl(url.pathname + url.search + url.hash);
  }

  // --- Saves that come back to this page -----------------------------------------------

  let kept = null; // what to put back after the swap

  function keepState() {
    return {
      y: window.scrollY,
      open: [...document.querySelectorAll("details[id][open]")]
        .filter((el) => !el.closest("dialog"))
        .map((el) => el.id),
      hidden: [...document.querySelectorAll("[data-keep-hidden][id]")].map((el) => [
        el.id,
        el.classList.contains("hidden"),
      ]),
    };
  }

  function restoreState(state) {
    state.open.forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.tagName === "DETAILS") el.open = true;
    });
    state.hidden.forEach(([id, hidden]) => {
      const el = document.getElementById(id);
      if (el && el.hasAttribute("data-keep-hidden")) el.classList.toggle("hidden", hidden);
    });
    if (Math.abs(window.scrollY - state.y) > 1) window.scrollTo(0, state.y);
    // The page's notice is at the top; if that's out of view, say it where you are.
    const flash = document.querySelector("#main-content > [role=status], #main-content > [role=alert]");
    if (flash && flash.getBoundingClientRect().bottom < 0) {
      toast(flash.textContent.trim(), flash.getAttribute("role") === "alert");
    }
  }

  function isStay(detail) {
    const config = detail.requestConfig;
    if (!config || detail.target !== document.body) return false;
    if (config.verb === "get") return Boolean(config.headers && config.headers["X-Kash-Stay"]);
    const landed = detail.pathInfo.responsePath || detail.pathInfo.finalRequestPath;
    return new URL(landed, location.href).pathname === location.pathname;
  }

  document.addEventListener("htmx:beforeSwap", (e) => {
    kept = null;
    if (!e.detail.shouldSwap || !isStay(e.detail)) return;
    e.detail.swapOverride = "innerHTML show:none transition:false";
    kept = keepState();
  });

  // Scroll back right away; open/collapsed states once htmx has settled (settling puts the
  // new elements' classes back, which would undo a collapsed group).
  document.addEventListener("htmx:afterSwap", (e) => {
    if (kept && e.detail.target === document.body) window.scrollTo(0, kept.y);
  });

  document.addEventListener("htmx:afterSettle", (e) => {
    if (!kept || e.detail.target !== document.body) return;
    const state = kept;
    kept = null;
    restoreState(state);
  });

  // A save that lands on the same page replaces the history entry instead of adding one.
  document.addEventListener("htmx:beforeHistoryUpdate", (e) => {
    const d = e.detail;
    const config = d.requestConfig;
    if (d.history.type !== "push" || !config || config.verb === "get") return;
    if (new URL(d.history.path, location.href).pathname === location.pathname) {
      d.history.type = "replace";
    }
  });

  // --- Theme and text size --------------------------------------------------------------

  // They live on <html>, which a swap keeps: take them (and the status-bar colour) from each
  // new page, so a Settings → Display save shows at once. "Follow system" follows it live.
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  function followSystem() {
    const html = document.documentElement;
    if (html.dataset.themeSetting === "system") {
      html.setAttribute("data-theme", systemDark.matches ? "dark" : "light");
    }
  }
  systemDark.addEventListener("change", followSystem);

  document.addEventListener("htmx:beforeSwap", (e) => {
    const page = e.detail.serverResponse;
    if (!e.detail.shouldSwap || e.detail.target !== document.body || typeof page !== "string") return;
    const tag = /<html\b([^>]*)>/i.exec(page.slice(0, 2000));
    if (!tag) return;
    const attr = (name) => (new RegExp(`\\s${name}="([^"]*)"`).exec(tag[1]) || [])[1];
    const html = document.documentElement;
    html.dataset.themeSetting = attr("data-theme-setting") || html.dataset.themeSetting;
    html.setAttribute("data-theme", attr("data-theme") || "light");
    html.style.fontSize = (/font-size:\s*([\d.]+%)/.exec(attr("style") || "") || [])[1] || "";
    followSystem();
    const head = page.slice(0, page.search(/<\/head>/i));
    const colors = [...head.matchAll(/<meta name="theme-color"[^>]*>/g)].map((m) => m[0]).join("");
    if (colors) {
      document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.remove());
      document.head.insertAdjacentHTML("beforeend", colors);
    }
  });

  // --- Page transitions ----------------------------------------------------------------

  // Animate only moving to another page; updates in place and Back stay instant.
  document.addEventListener("htmx:beforeTransition", (e) => {
    const d = e.detail || {};
    const config = d.requestConfig;
    const navigation = d.target === document.body && config && config.verb === "get";
    if (!navigation || reduceMotion.matches) e.preventDefault();
  });

  // A page change interrupted by the next one (or a hidden tab) skips its animation; htmx
  // doesn't catch that, so the browser would log an error for something harmless.
  window.addEventListener("unhandledrejection", (e) => {
    const reason = e.reason;
    if (reason && reason.name === "AbortError" && /transition/i.test(reason.message)) {
      e.preventDefault();
    }
  });

  // --- Back and Forward: fresh from the server, at the same scroll position ------------

  const SCROLLS = "kash-scroll";
  let current = pageKey(location.href); // the page shown now (htmx's own note can lag)

  function scrollMap() {
    try {
      return JSON.parse(sessionStorage.getItem(SCROLLS)) || {};
    } catch (_) {
      return {};
    }
  }

  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  document.addEventListener("htmx:beforeHistorySave", () => {
    const map = scrollMap();
    delete map[current];
    map[current] = Math.round(window.scrollY);
    const keys = Object.keys(map);
    keys.slice(0, Math.max(0, keys.length - 50)).forEach((key) => delete map[key]);
    try {
      sessionStorage.setItem(SCROLLS, JSON.stringify(map));
    } catch (_) {
      // private mode or full: Back just starts at the top
    }
  });

  ["htmx:pushedIntoHistory", "htmx:replacedInHistory"].forEach((name) =>
    document.addEventListener(name, () => {
      current = pageKey(location.href);
      dropFlashFromUrl();
    })
  );

  // A Back that lands on the login page (the session ended): load it as a real page.
  document.addEventListener("htmx:historyCacheMissLoad", (e) => {
    const xhr = e.detail.xhr;
    if (!xhr.responseURL) return;
    const landed = new URL(xhr.responseURL);
    if (landed.pathname !== new URL(e.detail.path, location.href).pathname) {
      e.detail.response = "";
      location.replace(landed.href);
    }
  });

  document.addEventListener("htmx:historyRestore", () => {
    current = pageKey(location.href);
    window.scrollTo(0, scrollMap()[current] || 0);
  });

  document.addEventListener("htmx:historyCacheMissLoadError", () => location.reload());

  // --- Forms whose address a script sets ----------------------------------------------

  // htmx notes a boosted form's action when the page loads; pop-up forms get theirs from a
  // script when they open (e.g. /transactions/<id>/category), so send to the current one.
  document.addEventListener("htmx:configRequest", (e) => {
    const form = e.detail.elt;
    if (!e.detail.boosted || !(form instanceof HTMLFormElement) || e.detail.verb === "get") return;
    const action = form.getAttribute("action");
    if (action) e.detail.path = action;
  });

  // --- No double submits ---------------------------------------------------------------

  document.addEventListener("htmx:beforeRequest", (e) => {
    const form = e.detail.elt;
    if (!(form instanceof HTMLFormElement) || e.detail.requestConfig.verb === "get") return;
    // Already sending, or left over from a page that has been replaced
    if (form.hasAttribute("data-busy") || !document.body.contains(form)) {
      e.preventDefault();
      return;
    }
    form.setAttribute("data-busy", "");
    [...form.elements].forEach((el) => {
      if (el.type === "submit" && !el.disabled) {
        el.disabled = true;
        el.setAttribute("data-busy-disabled", "");
      }
    });
  });

  document.addEventListener("htmx:afterRequest", (e) => {
    const form = e.detail.elt;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-busy")) return;
    form.removeAttribute("data-busy");
    [...form.elements].forEach((el) => {
      if (el.hasAttribute("data-busy-disabled")) {
        el.disabled = false;
        el.removeAttribute("data-busy-disabled");
      }
    });
  });

  // --- Pop-ups ------------------------------------------------------------------------

  // Tapping the shaded area around a pop-up closes it, like Esc. Only a tap that starts
  // there too: selecting text in a field and letting go outside keeps the pop-up open.
  let pressedOn = null;
  document.addEventListener("pointerdown", (e) => {
    pressedOn = e.target;
  });
  document.addEventListener("click", (e) => {
    const dialog = e.target;
    if (dialog instanceof HTMLDialogElement && dialog.classList.contains("modal")) {
      if (dialog.open && pressedOn === dialog) dialog.close();
    }
    pressedOn = null;
  });

  // On a phone, opening a pop-up doesn't bring up the keyboard: the browser would focus
  // its first field, so the pop-up itself gets focus instead. A page that wants a field
  // (the budget amount) focuses it after opening, which still works.
  const coarsePointer = window.matchMedia("(pointer: coarse)");
  const showModal = HTMLDialogElement.prototype.showModal;
  HTMLDialogElement.prototype.showModal = function () {
    showModal.call(this);
    const field = document.activeElement;
    const typing = "textarea, input:not([type=checkbox]):not([type=radio]):not([type=file])";
    if (coarsePointer.matches && this.contains(field) && field.matches(typing)) {
      const box = this.querySelector(".modal-box, .kash-sheet-panel") || this;
      box.tabIndex = -1;
      box.focus({ preventScroll: true });
    }
  };

  // Below 768px pop-ups are bottom sheets (app.css): drag one down to close it. The drag
  // starts anywhere on the sheet when what's under the finger is scrolled to the top, but
  // not on a field (or a Customize reorder handle); it closes past a third or on a flick.
  const sheetLayout = window.matchMedia("(max-width: 767.98px)");

  function closeSheet(dialog, panel) {
    // Like Esc: the Customize panel runs its own closing (cancel event); others just close
    if (dialog.dispatchEvent(new Event("cancel", { cancelable: true }))) dialog.close();
    setTimeout(() => {
      panel.style.transition = "";
      panel.style.transform = ""; // closed sheets sit below the screen anyway (app.css)
    }, 250);
  }

  function bindSwipe(dialog) {
    if (dialog.kashSwipe) return;
    dialog.kashSwipe = true;
    let drag = null;
    dialog.addEventListener(
      "touchstart",
      (e) => {
        drag = null;
        if (!dialog.open || !sheetLayout.matches || e.touches.length !== 1) return;
        const panel = e.target.closest(".modal-box, .kash-sheet-panel");
        if (!panel || e.target.closest("input, select, textarea, [contenteditable], .kc-handle")) {
          return;
        }
        for (let el = e.target; el && el !== dialog; el = el.parentElement) {
          if (el.scrollTop > 0) return; // scrolled down: this touch scrolls
        }
        const y = e.touches[0].clientY;
        drag = { panel, x0: e.touches[0].clientX, y0: y, y, t: e.timeStamp, speed: 0, on: false };
      },
      { passive: true }
    );
    dialog.addEventListener(
      "touchmove",
      (e) => {
        if (!drag) return;
        const x = e.touches[0].clientX;
        const y = e.touches[0].clientY;
        const dy = y - drag.y0;
        if (!drag.on) {
          if (Math.abs(dy) < 8 && Math.abs(x - drag.x0) < 8) return;
          if (dy <= 0 || Math.abs(x - drag.x0) > dy) {
            drag = null; // scrolling up or sideways
            return;
          }
          drag.on = true;
          drag.panel.style.transition = "none";
        }
        e.preventDefault();
        drag.speed = (y - drag.y) / Math.max(1, e.timeStamp - drag.t);
        drag.y = y;
        drag.t = e.timeStamp;
        drag.panel.style.transform = `translateY(${Math.max(0, dy)}px)`;
      },
      { passive: false }
    );
    const end = () => {
      if (!drag || !drag.on) {
        drag = null;
        return;
      }
      const { panel, y, y0, speed } = drag;
      drag = null;
      panel.style.transition = "transform 0.2s ease-out";
      if (y - y0 > panel.offsetHeight / 3 || speed > 0.6) {
        panel.style.transform = "translateY(100%)";
        setTimeout(() => closeSheet(dialog, panel), 200);
      } else {
        panel.style.transform = "";
        setTimeout(() => (panel.style.transition = ""), 200);
      }
    };
    dialog.addEventListener("touchend", end);
    dialog.addEventListener("touchcancel", end);
  }

  htmx.onLoad((root) => {
    if (root.matches && root.matches("dialog")) bindSwipe(root);
    if (root.querySelectorAll) root.querySelectorAll("dialog.modal, dialog.kash-sheet").forEach(bindSwipe);
  });

  // --- Errors --------------------------------------------------------------------------

  function toast(message, isError) {
    let box = document.getElementById("kash-toast");
    if (!box) {
      box = document.createElement("div");
      box.id = "kash-toast";
      box.setAttribute("role", "alert");
      document.body.appendChild(box);
    }
    box.textContent = message;
    box.classList.toggle("kash-toast-error", Boolean(isError));
    box.classList.add("kash-toast-show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => box.classList.remove("kash-toast-show"), 6000);
  }
  window.kashToast = toast;

  document.addEventListener("htmx:responseError", (e) => {
    const status = e.detail.xhr.status;
    if (status === 404) toast("That page doesn't exist.", true);
    else if (status === 403) toast("Kash refused that request. Reload the page and try again.", true);
    else if (status === 422) toast("Some details weren't valid. Check the form and try again.", true);
    else toast(`Something went wrong (error ${status}). Try again.`, true);
  });

  document.addEventListener("htmx:sendError", () =>
    toast("Can't reach Kash. Check your connection and try again.", true)
  );

  // --- Push notifications --------------------------------------------------------------

  // The browser holds this device's subscription; the server keeps a copy to send to.
  // Settings shows the one state that applies (data-push-state in settings.html).
  const PUSH_SAVED = "kash-push-saved";

  function pushCapable() {
    return window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window;
  }

  // serviceWorker.ready never settles if the worker failed to install.
  function withTimeout(promise, ms) {
    return Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error("timed out")), ms)),
    ]);
  }

  function pushPost(path, fields, options) {
    return fetch(path, { method: "POST", body: new URLSearchParams(fields), ...options });
  }

  function saveSubscription(subscription) {
    const { endpoint, keys } = subscription.toJSON();
    return pushPost("/notifications/subscribe", { endpoint, p256dh: keys.p256dh, auth: keys.auth });
  }

  function dropSubscription(subscription, options) {
    const endpoint = subscription.endpoint;
    return subscription.unsubscribe().then(() => pushPost("/notifications/unsubscribe", { endpoint }, options));
  }

  function keyBytes(key) {
    return Uint8Array.from(atob(key.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  }

  // False after Settings → Reset: the subscription is for the keys that were replaced.
  function subscribedWithKey(subscription, key) {
    const current = subscription.options.applicationServerKey;
    return !current || new Uint8Array(current).join() === keyBytes(key).join();
  }

  async function pushState(card) {
    if (!window.isSecureContext) return "https";
    const apple =
      /iphone|ipad|ipod/i.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    if (apple && !navigator.standalone) return "install";
    if (!pushCapable()) return "unsupported";
    if (Notification.permission === "denied") return "denied";
    if (!card.dataset.pushKey) return "unreadable";
    const registration = await withTimeout(navigator.serviceWorker.ready, 5000);
    const subscription = await registration.pushManager.getSubscription();
    return subscription && subscribedWithKey(subscription, card.dataset.pushKey) ? "on" : "off";
  }

  async function showPushState(card) {
    const state = await pushState(card).catch(() => "unsupported");
    card.querySelectorAll("[data-push-state]").forEach((el) =>
      el.classList.toggle("hidden", el.dataset.pushState !== state)
    );
  }

  const pushActions = {
    async on(card) {
      // First, with nothing awaited before it: browsers only ask straight after a tap.
      if ((await Notification.requestPermission()) !== "granted") return;
      const registration = await withTimeout(navigator.serviceWorker.ready, 5000);
      const previous = await registration.pushManager.getSubscription();
      if (previous) await previous.unsubscribe();
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: keyBytes(card.dataset.pushKey),
      });
      const response = await saveSubscription(subscription);
      if (!response.ok) {
        await subscription.unsubscribe();
        throw new Error("not saved");
      }
      toast("Notifications are on for this device.");
    },
    async off() {
      const registration = await withTimeout(navigator.serviceWorker.ready, 5000);
      const subscription = await registration.pushManager.getSubscription();
      if (subscription) await dropSubscription(subscription);
      toast("Notifications are off for this device.");
    },
    async test() {
      const response = await pushPost("/notifications/test", {});
      const { sent } = await response.json();
      if (sent) toast(sent === 1 ? "Test sent to your device." : `Test sent to ${sent} devices.`);
      else toast("The test didn't go out. Check that the server can reach the internet.", true);
    },
  };

  document.addEventListener("click", async (event) => {
    const button = event.target.closest && event.target.closest("[data-push-action]");
    if (!button) return;
    const card = button.closest("[data-push-card]");
    button.disabled = true;
    try {
      await pushActions[button.dataset.pushAction](card);
    } catch (error) {
      toast("That didn't work on this device. Try again.", true);
    }
    button.disabled = false;
    await showPushState(card);
  });

  htmx.onLoad((root) => {
    if (root.querySelectorAll) root.querySelectorAll("[data-push-card]").forEach(showPushState);
  });

  // Logging out stops this device's notifications, so a shared phone doesn't keep getting
  // someone's alerts. Logout still goes through if this is slow or fails.
  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (new URL(form.action, location.href).pathname !== "/logout" || !pushCapable()) return;
      event.preventDefault();
      const stop = navigator.serviceWorker
        .getRegistration()
        .then((registration) => registration && registration.pushManager.getSubscription())
        .then((subscription) => subscription && dropSubscription(subscription, { keepalive: true }));
      withTimeout(stop, 1000)
        .catch(() => {})
        .finally(() => form.submit());
    },
    true
  );

  // Push services can replace a device's address, so tell the server once per session
  // (this also moves a device to whoever logged in on it).
  async function refreshSubscription() {
    if (!pushCapable() || !document.querySelector(".kash-sidebar")) return;
    try {
      if (sessionStorage.getItem(PUSH_SAVED)) return;
      const registration = await navigator.serviceWorker.getRegistration();
      const subscription = registration && (await registration.pushManager.getSubscription());
      if (subscription && (await saveSubscription(subscription)).ok) {
        sessionStorage.setItem(PUSH_SAVED, "1");
      }
    } catch (error) {
      // Private mode or no connection: nothing to do until the next visit.
    }
  }

  // First load (or a full reload): remember this page and tidy the address.
  dropFlashFromUrl();
  window.addEventListener("load", refreshSubscription);
})();
