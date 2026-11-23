// The console's push worker (ADR 0135 W2, "Transport by device class" and "What leaves the
// machine"). Served at /app/sw.js, so its scope is /app/.
//
// It handles `push` and `notificationclick` and nothing else. There is deliberately NO `fetch`
// handler: the worker never sees a console request, so it cannot cache an API response or a
// stale bundle. It never calls the hub and holds no credential — a click only focuses or
// opens a console window, which authenticates and resolves the notice itself.
//
// Payload, version 1 (JSON, at most 2 KiB before encryption; the sender builds the same shape):
//   { "v": 1,
//     "id": "<opaque notice id>",
//     "kind": "notify" | "question" | "review",
//     "importance": "low" | "normal" | "high",
//     "created_at": <ISO 8601 string or epoch seconds>,
//     "expires_at": <ISO 8601 string or epoch seconds>,
//     "title": "protoAgent needs your attention",
//     "body": "Open the console to view a decision",
//     "route": "/app/?notice=<id>" }
// Anything else — no data, not JSON, another version, an unknown kind, oversized text — shows
// the generic fallback below. A push always shows a notification (browsers require it).
"use strict";

var PAYLOAD_VERSION = 1;
var MAX_PAYLOAD_CHARS = 4096;
var MAX_TITLE_CHARS = 120;
var MAX_BODY_CHARS = 240;
var FALLBACK_TITLE = "protoAgent needs your attention";
var FALLBACK_BODY = "Open the console to view an update";
var FALLBACK_TAG = "protoagent-notice";
var CONSOLE_ROOT = "/app/";
var ICON = "/app/icons/icon-192.png";
var CLICK_MESSAGE = "protoagent:notice-click";
// Where the last clicked notice waits for the console (see `rememberClick`). Not an offline
// cache: one small entry, no request is ever served from it.
var CLICK_CACHE = "protoagent-notice-click";
var CLICK_KEY = "/app/__notice-click";
// What happened to the last few taps, for Settings > Devices (lib/noticeTrace.ts). A tap on an
// installed phone console cannot be observed any other way. Step names, opaque ids and page
// paths only.
var TRACE_CACHE = "protoagent-notice-trace";
var TRACE_KEY = "/app/__notice-trace";
var TRACE_MAX = 40;
var KINDS = ["notify", "question", "review"];
var IMPORTANCES = ["low", "normal", "high"];
var NOTICE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
// The console routes a notice may open: the console itself, or one agent's window in it.
var CONSOLE_PATH = /^\/app\/(?:agent\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\/)?$/;

/** A known same-origin console route as `path[?notice=<id>]`, or null for anything else. */
function consoleRoute(route, id) {
  if (typeof route !== "string" || route.length > 512 || route.indexOf(CONSOLE_ROOT) !== 0) return null;
  var url;
  try {
    url = new URL(route, self.location.origin);
  } catch (e) {
    return null;
  }
  if (url.origin !== self.location.origin || url.hash || !CONSOLE_PATH.test(url.pathname)) return null;
  var keys = Array.from(url.searchParams.keys());
  if (keys.length === 0) return url.pathname;
  var notice = url.searchParams.get("notice");
  if (keys.length !== 1 || keys[0] !== "notice" || notice !== id || !NOTICE_ID.test(notice)) return null;
  return url.pathname + "?notice=" + encodeURIComponent(notice);
}

function boundedText(value, max) {
  return typeof value === "string" && value.trim() !== "" && value.length <= max;
}

function timeMs(value) {
  var ms = typeof value === "number" ? value * 1000 : typeof value === "string" ? Date.parse(value) : NaN;
  return isFinite(ms) && ms > 0 ? ms : null;
}

/** The notice in a push message's text, or null when it is not a version-1 payload. */
function parseNotice(text) {
  if (typeof text !== "string" || text === "" || text.length > MAX_PAYLOAD_CHARS) return null;
  var doc;
  try {
    doc = JSON.parse(text);
  } catch (e) {
    return null;
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc) || doc.v !== PAYLOAD_VERSION) return null;
  if (typeof doc.id !== "string" || !NOTICE_ID.test(doc.id)) return null;
  if (KINDS.indexOf(doc.kind) === -1 || IMPORTANCES.indexOf(doc.importance) === -1) return null;
  if (!boundedText(doc.title, MAX_TITLE_CHARS) || !boundedText(doc.body, MAX_BODY_CHARS)) return null;
  return {
    id: doc.id,
    title: doc.title,
    body: doc.body,
    createdMs: timeMs(doc.created_at),
    // A route that is not a known console route is dropped, not followed: the click still
    // lands in the console, where the operator finds the notice.
    route: consoleRoute(doc.route, doc.id) || CONSOLE_ROOT,
  };
}

/** Show one notification for a push message (`data` is a PushMessageData, or null). */
function showNotice(data) {
  var text = "";
  try {
    text = data ? data.text() : "";
  } catch (e) {
    text = "";
  }
  var notice = parseNotice(text);
  var options = {
    body: notice ? notice.body : FALLBACK_BODY,
    // One notification per notice id: a repeat delivery replaces the first.
    tag: notice ? notice.id : FALLBACK_TAG,
    icon: ICON,
    badge: ICON,
    data: { v: PAYLOAD_VERSION, id: notice ? notice.id : "", route: notice ? notice.route : CONSOLE_ROOT },
  };
  if (notice && notice.createdMs) options.timestamp = notice.createdMs;
  return self.registration.showNotification(notice ? notice.title : FALLBACK_TITLE, options);
}

/** Focus the console window already on `route`'s page and tell it which notice was clicked;
 *  with none open (or when it cannot be focused), open one. */
/** Append one step to the tap trace. Never rejects. Writes are queued: each is a
 *  read-modify-write of one entry, so two at once would lose a step. */
var traceQueue = Promise.resolve();
function trace(step, detail) {
  var at = Date.now();
  traceQueue = traceQueue.then(function () {
    return writeTrace(at, step, detail);
  });
  return traceQueue;
}

function writeTrace(at, step, detail) {
  if (typeof caches === "undefined") return Promise.resolve();
  return caches
    .open(TRACE_CACHE)
    .then(function (cache) {
      return cache
        .match(TRACE_KEY)
        .then(function (hit) {
          return hit ? hit.json() : [];
        })
        .catch(function () {
          return [];
        })
        .then(function (rows) {
          var list = Array.isArray(rows) ? rows : [];
          list.push({ at: at, who: "worker", step: step, detail: String(detail || "").slice(0, 160) });
          return cache.put(TRACE_KEY, new Response(JSON.stringify(list.slice(-TRACE_MAX))));
        });
    })
    .catch(function () {});
}

function pathOf(url) {
  try {
    return new URL(url).pathname;
  } catch (e) {
    return "?";
  }
}

function openConsole(route, id) {
  var target = new URL(route, self.location.origin);
  return self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (windows) {
    trace("windows", windows.length + " open: " + windows.map(function (w) { return pathOf(w.url) + (w.focused ? "*" : ""); }).join(" "));
    var consoles = windows.filter(function (client) {
      try {
        var at = new URL(client.url);
        return at.origin === target.origin && CONSOLE_PATH.test(at.pathname);
      } catch (e) {
        return false;
      }
    });
    // A window already on the route's page first; else any open console (the focused one,
    // else the first): it gets the same message, so the operator keeps one console instead
    // of a second one starting beside it.
    var open = consoles.filter(function (client) {
      return new URL(client.url).pathname === target.pathname;
    })[0];
    var which = "route's page";
    if (!open) {
      open = consoles.filter(function (client) { return client.focused; })[0] || consoles[0];
      which = "other page";
    }
    if (!open) {
      return self.clients.openWindow(target.href).then(
        function (client) {
          trace("open-window", client ? "opened " + pathOf(client.url) : "returned nothing");
          return client;
        },
        function (error) {
          return trace("open-window", "refused: " + (error && error.name));
        },
      );
    }
    return open.focus().then(
      function (focused) {
        (focused || open).postMessage({ type: CLICK_MESSAGE, v: PAYLOAD_VERSION, id: id, route: route });
        trace("message", "sent to " + pathOf(open.url) + " (" + which + ")");
      },
      function (error) {
        trace("focus", "refused: " + (error && error.name));
        return self.clients.openWindow(target.href);
      },
    );
  });
}

/** The route a clicked notification may open. Its data is checked again here: the route was
 *  stored by this worker, but nothing else is ever followed. */
function clickRoute(data) {
  var id = data && typeof data.id === "string" ? data.id : "";
  return (data && consoleRoute(data.route, id)) || CONSOLE_ROOT;
}

function onPush(event) {
  event.waitUntil(showNotice(event.data));
}

/** Leave the clicked notice's id where the console will find it when it next comes to the
 *  front. Opening a window on a route and messaging an open one both depend on the platform
 *  handing the click over; an installed iOS web app can come to the front on whatever page it
 *  was left on, with neither. One entry, the opaque id and a time only; the console deletes
 *  it on read (`NoticeClickWatch`). Never rejects: the click must still open the console. */
function rememberClick(id) {
  if (!id || !NOTICE_ID.test(id) || typeof caches === "undefined") return trace("note", "not written: no usable id");
  var body = JSON.stringify({ v: PAYLOAD_VERSION, id: id, at: Date.now() });
  return caches
    .open(CLICK_CACHE)
    .then(function (cache) {
      return cache.put(CLICK_KEY, new Response(body, { headers: { "Content-Type": "application/json" } }));
    })
    .then(
      function () {
        return trace("note", "written");
      },
      function (error) {
        return trace("note", "failed: " + (error && error.name));
      },
    );
}

function onNotificationClick(event) {
  var data = event.notification.data;
  var id = data && typeof data.id === "string" ? data.id : "";
  event.notification.close();
  event.waitUntil(
    trace("tap", (id || "no id") + " → " + clickRoute(data))
      .then(function () {
        return rememberClick(id);
      })
      .then(function () {
        return openConsole(clickRoute(data), id);
      })
      .then(function () {
        return traceQueue; // keep the worker alive until every step is written
      }),
  );
}

self.addEventListener("push", onPush);
self.addEventListener("notificationclick", onNotificationClick);

// A new version of this worker takes over at once instead of waiting for every console
// window to close: an installed phone console can stay open for days, and it would keep
// answering clicks with the old handler. Safe here because there is no `fetch` handler, so
// no open page depends on which version is active.
self.skipWaiting();
