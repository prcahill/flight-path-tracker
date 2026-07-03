/* Client-side playback engine for the Flight Path Tracker.
 *
 * Playback runs entirely in the browser: a requestAnimationFrame loop
 * interpolates the aircraft state between (decimated) samples shipped once
 * in the `engine-data` store, writes the position straight into the MapLibre
 * GeoJSON sources, and updates the readout DOM directly. No network traffic
 * occurs during playback, so the app works identically on localhost and on
 * a remote host regardless of latency. The server is only involved in
 * parsing uploaded CSVs.
 */
"use strict";

window.FT = (function () {
  var d = null;          // engine data: {t, lat, lon, alt, gs, hdg, vs, dist, meta}
  var playing = false;
  var speed = 25;
  var follow = false;
  var curTime = 0;
  var anchorWall = 0;    // performance.now() ms at last (re)anchor
  var anchorData = 0;    // data seconds at last (re)anchor
  var rafId = null;
  var lastSync = 0;       // last slider/profile sync, ms
  var lastSyncValue = null;  // exact value we last wrote to the slider

  function gd() {
    var el = document.getElementById("map");
    return el && el.querySelector(".js-plotly-plot");
  }

  function profilesGd() {
    var el = document.getElementById("profiles");
    return el && el.querySelector(".js-plotly-plot");
  }

  function mapObj() {
    var g = gd();
    var sp = g && g._fullLayout && g._fullLayout.map && g._fullLayout.map._subplot;
    return sp && sp.map;
  }

  // -- helpers ------------------------------------------------------------
  function bisect(arr, x) {
    var lo = 0, hi = arr.length - 1;
    if (x <= arr[0]) return 0;
    if (x >= arr[hi]) return hi - 1;
    while (hi - lo > 1) {
      var mid = (lo + hi) >> 1;
      if (arr[mid] <= x) lo = mid; else hi = mid;
    }
    return lo;
  }

  function sample(t) {
    var i = bisect(d.t, t);
    var j = i + 1;
    var dt = d.t[j] - d.t[i];
    var w = dt > 0 ? (t - d.t[i]) / dt : 0;
    w = Math.max(0, Math.min(1, w));
    var lerp = function (a) { return a[i] + (a[j] - a[i]) * w; };
    var dh = ((d.hdg[j] - d.hdg[i] + 540) % 360) - 180;   // shortest arc
    return {
      t: t, lat: lerp(d.lat), lon: lerp(d.lon), alt: lerp(d.alt),
      gs: lerp(d.gs), vs: lerp(d.vs), dist: lerp(d.dist),
      hdg: ((d.hdg[i] + dh * w) % 360 + 360) % 360,
    };
  }

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec));
    var p = function (n) { return String(n).padStart(2, "0"); };
    return p(Math.floor(sec / 3600)) + ":" + p(Math.floor((sec % 3600) / 60)) + ":" + p(sec % 60);
  }

  var COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];

  function fmtInt(x) { return Math.round(x).toLocaleString("en-US"); }

  function setText(id, txt) {
    var el = document.getElementById(id);
    if (el) el.textContent = txt;
  }

  function setProps(id, props) {
    if (window.dash_clientside && window.dash_clientside.set_props) {
      window.dash_clientside.set_props(id, props);
    }
  }

  // -- rendering ----------------------------------------------------------
  function drawMarker(s) {
    var g = gd();
    if (!g || !g._fullData || g._fullData.length < 4) return;
    var m = mapObj();
    if (!m) return;
    var fc = {type: "FeatureCollection", features: [{type: "Feature", id: 1,
              geometry: {type: "Point", coordinates: [s.lon, s.lat]}, properties: {}}]};
    try {
      var halo = m.getSource("source-" + g._fullData[2].uid + "-circle");
      var dot = m.getSource("source-" + g._fullData[3].uid + "-circle");
      if (halo && dot) {
        halo.setData(fc);
        dot.setData(fc);
        g.data[2].lat = [s.lat]; g.data[2].lon = [s.lon];
        g.data[3].lat = [s.lat]; g.data[3].lon = [s.lon];
      }
    } catch (e) { /* map still initializing */ }
    if (follow) {
      m.jumpTo({center: [s.lon, s.lat]});
      if (g.layout.map) g.layout.map.center = {lat: s.lat, lon: s.lon};
      g._fullLayout.map.center = {lat: s.lat, lon: s.lon};
    }
  }

  function drawReadout(s) {
    var timeStr;
    if (d.meta.t0_ms != null) {
      timeStr = new Date(d.meta.t0_ms + s.t * 1000).toISOString().replace("T", " ").slice(0, 19);
    } else {
      timeStr = fmtDur(s.t);
    }
    setText("rd-time", timeStr);
    setText("rd-pos", s.lat.toFixed(4) + "°, " + s.lon.toFixed(4) + "°");
    setText("rd-alt", fmtInt(s.alt) + " ft");
    setText("rd-gs", Math.round(s.gs) + " kt");
    setText("rd-hdg", String(Math.round(s.hdg)).padStart(3, "0") + "°  " +
            COMPASS[Math.round(s.hdg / 22.5) % 16]);
    setText("rd-vs", fmtInt(s.vs) + " ft/min");
    setText("rd-dist", s.dist.toFixed(1) + " nm");
    setText("clock", fmtDur(s.t) + " / " + fmtDur(d.meta.duration_s));
  }

  function drawCursors(s) {
    var pg = profilesGd();
    if (!pg || !pg.data || pg.data.length < 4) return;
    try {
      window.Plotly.restyle(pg, {x: [[s.t / 60], [s.t / 60]], y: [[s.alt], [s.gs]]}, [1, 3]);
    } catch (e) { /* profiles not ready */ }
  }

  function render(s, opts) {
    opts = opts || {};
    drawMarker(s);
    drawReadout(s);
    if (opts.cursors) drawCursors(s);
    // Only the playback loop may write the slider. Writing it from seek()
    // would re-fire the scrub callback -> seek() -> infinite loop. The exact
    // written value is remembered so its callback echo can be ignored.
    if (opts.slider) {
      lastSyncValue = s.t;
      setProps("scrub", {value: s.t});
    }
  }

  // -- main loop ----------------------------------------------------------
  function tick(nowMs) {
    rafId = null;
    if (!d || !playing) return;
    var t = anchorData + ((nowMs - anchorWall) / 1000) * speed;
    var ended = t >= d.meta.duration_s;
    if (ended) t = d.meta.duration_s;
    curTime = t;
    var syncSlow = nowMs - lastSync > 250;
    if (syncSlow) lastSync = nowMs;
    render(sample(t), {cursors: syncSlow || ended, slider: syncSlow || ended});
    if (ended) {
      playing = false;
      setProps("play", {children: "▶  PLAY"});
      return;
    }
    rafId = window.requestAnimationFrame(tick);
  }

  function anchor() {
    anchorWall = performance.now();
    anchorData = curTime;
  }

  // -- public API (called from dash clientside callbacks) ------------------
  return {
    load: function (data) {
      d = data;
      playing = false;
      curTime = 0;
      if (rafId) { window.cancelAnimationFrame(rafId); rafId = null; }
      if (d) render(sample(0), {cursors: true});
    },
    togglePlay: function () {
      if (!d) return "▶  PLAY";
      if (playing) {
        playing = false;
        return "▶  PLAY";
      }
      if (curTime >= d.meta.duration_s) curTime = 0;   // replay from start
      playing = true;
      anchor();
      lastSync = 0;
      if (!rafId) rafId = window.requestAnimationFrame(tick);
      return "❚❚  PAUSE";
    },
    setSpeed: function (mult) {
      anchor();                       // re-anchor so speed changes don't jump
      speed = mult || 1;
    },
    seek: function (t) {
      if (!d) return;
      var raw = +t || 0;
      // Ignore the callback echo of our own slider sync. The slider may snap
      // the written value to its step grid, so tolerate up to one step.
      var step = d.meta.duration_s / 2000;
      if (lastSyncValue !== null && Math.abs(raw - lastSyncValue) <= step + 1e-9) return;
      var tt = Math.max(0, Math.min(d.meta.duration_s, raw));
      if (Math.abs(tt - curTime) < 0.05) return;
      curTime = tt;
      anchor();
      render(sample(curTime), {cursors: true});
    },
    setFollow: function (on) {
      follow = !!on;
      var m = mapObj();
      var g = gd();
      if (!m || !d) return;
      if (follow) {
        var s = sample(curTime);
        m.jumpTo({center: [s.lon, s.lat], zoom: d.meta.follow_zoom});
      } else {
        m.jumpTo({center: [d.meta.center_lon, d.meta.center_lat], zoom: d.meta.fit_zoom});
      }
      if (g && g.layout.map) {
        g.layout.map.center = {lat: m.getCenter().lat, lon: m.getCenter().lng};
        g.layout.map.zoom = m.getZoom();
        g._fullLayout.map.center = g.layout.map.center;
        g._fullLayout.map.zoom = m.getZoom();
      }
    },
    setBasemap: function (style) {
      var g = gd();
      if (g) window.Plotly.relayout(g, {"map.style": style});
    },
  };
})();
