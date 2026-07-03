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
    // Optional channels may be absent (null array) or have null entries
    // where the source had not yet reported them.
    var lerpOpt = function (a) {
      if (!a || a[i] == null || a[j] == null) return null;
      return a[i] + (a[j] - a[i]) * w;
    };
    var dh = ((d.hdg[j] - d.hdg[i] + 540) % 360) - 180;   // shortest arc
    return {
      t: t, lat: lerp(d.lat), lon: lerp(d.lon), alt: lerp(d.alt),
      gs: lerp(d.gs), vs: lerp(d.vs), dist: lerp(d.dist),
      hdg: ((d.hdg[i] + dh * w) % 360 + 360) % 360,
      pitch: lerpOpt(d.pitch),
      roll: lerpOpt(d.roll),
      slant: lerpOpt(d.slant),
      fcLat: lerpOpt(d.fc_lat),
      fcLon: lerpOpt(d.fc_lon),
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
  var EMPTY_FC = {type: "FeatureCollection", features: []};

  function pointFC(lon, lat, props) {
    return {type: "FeatureCollection", features: [{type: "Feature", id: 1,
            geometry: {type: "Point", coordinates: [lon, lat]},
            properties: props || {}}]};
  }

  function planeImage() {
    // Plane silhouette pointing north, drawn at 2x for crisp rendering.
    var cv = document.createElement("canvas");
    cv.width = 64; cv.height = 64;
    var ctx = cv.getContext("2d");
    var pts = [[0,-18],[2.5,-14],[3,-7],[17,3],[17,7],[3.5,4],[2.5,12],[7,16],
               [7,19],[0,17],[-7,19],[-7,16],[-2.5,12],[-3.5,4],[-17,7],
               [-17,3],[-3,-7],[-2.5,-14]];
    ctx.translate(32, 32);
    ctx.scale(1.5, 1.5);
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (var i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.closePath();
    ctx.fillStyle = "#FFFFFF";
    ctx.strokeStyle = "#0A1526";
    ctx.lineWidth = 2;
    ctx.fill();
    ctx.stroke();
    return ctx.getImageData(0, 0, 64, 64);
  }

  function ensureAircraftLayer(m) {
    // Heading-rotated aircraft symbol as a native MapLibre layer. Basemap
    // changes rebuild the style and drop custom layers, so this re-adds
    // itself idempotently (a style.load hook triggers the next attempt).
    try {
      if (m.getLayer("ft-ac")) return true;
      if (!m.isStyleLoaded()) return false;
      if (!m.hasImage("ft-plane")) m.addImage("ft-plane", planeImage(), {pixelRatio: 2});
      if (!m.getSource("ft-ac-src")) {
        m.addSource("ft-ac-src", {type: "geojson", data: EMPTY_FC});
      }
      m.addLayer({id: "ft-ac", type: "symbol", source: "ft-ac-src",
        layout: {"icon-image": "ft-plane", "icon-size": 0.72,
                 "icon-rotate": ["get", "hdg"], "icon-rotation-alignment": "map",
                 "icon-allow-overlap": true, "icon-ignore-placement": true}});
      return true;
    } catch (e) { return false; }
  }

  function drawStare(g, m, s) {
    if (g._fullData.length < 6) return;
    try {
      var lineSrc = m.getSource("source-" + g._fullData[4].uid + "-line");
      var ptSrc = m.getSource("source-" + g._fullData[5].uid + "-circle");
      if (!lineSrc || !ptSrc) return;
      if (s.fcLat == null || s.fcLon == null) {
        lineSrc.setData(EMPTY_FC);
        ptSrc.setData(EMPTY_FC);
        g.data[4].lat = []; g.data[4].lon = [];
        g.data[5].lat = []; g.data[5].lon = [];
        return;
      }
      lineSrc.setData({type: "FeatureCollection", features: [{type: "Feature", id: 1,
        geometry: {type: "LineString",
                   coordinates: [[s.lon, s.lat], [s.fcLon, s.fcLat]]},
        properties: {}}]});
      ptSrc.setData(pointFC(s.fcLon, s.fcLat));
      g.data[4].lat = [s.lat, s.fcLat]; g.data[4].lon = [s.lon, s.fcLon];
      g.data[5].lat = [s.fcLat]; g.data[5].lon = [s.fcLon];
    } catch (e) { /* map still initializing */ }
  }

  function drawMarker(s) {
    var g = gd();
    if (!g || !g._fullData || g._fullData.length < 4) return;
    var m = mapObj();
    if (!m) return;
    if (!m.__ftStyleHook) {
      m.__ftStyleHook = true;
      m.on("style.load", function () { ensureAircraftLayer(m); });
    }
    var fc = pointFC(s.lon, s.lat);
    var planeActive = ensureAircraftLayer(m);
    try {
      var halo = m.getSource("source-" + g._fullData[2].uid + "-circle");
      var dot = m.getSource("source-" + g._fullData[3].uid + "-circle");
      if (halo && dot) {
        halo.setData(fc);
        // The plotly dot is the fallback when the symbol layer is unavailable.
        dot.setData(planeActive ? EMPTY_FC : fc);
        g.data[2].lat = [s.lat]; g.data[2].lon = [s.lon];
        g.data[3].lat = [s.lat]; g.data[3].lon = [s.lon];
      }
      if (planeActive) {
        m.getSource("ft-ac-src").setData(pointFC(s.lon, s.lat, {hdg: s.hdg}));
      }
    } catch (e) { /* map still initializing */ }
    drawStare(g, m, s);
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
    if (s.pitch !== null) setText("rd-pitch", (s.pitch >= 0 ? "+" : "") + s.pitch.toFixed(1) + "°");
    if (s.roll !== null) setText("rd-roll", (s.roll >= 0 ? "+" : "") + s.roll.toFixed(1) + "°");
    if (d.slant) setText("rd-slant", s.slant == null ? "—" : fmtInt(s.slant) + " ft");
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
    segmentStats: function (sel) {
      /* Stats for a horizontally-selected time window on the profiles. */
      if (!d || !sel || !sel.range) return "";
      var r = sel.range.x || sel.range.x2;
      if (!r) return "";
      var t1 = Math.max(0, Math.min(r[0], r[1]) * 60);
      var t2 = Math.min(d.meta.duration_s, Math.max(r[0], r[1]) * 60);
      if (t2 - t1 < 1) return "";
      var i1 = bisect(d.t, t1), i2 = Math.min(bisect(d.t, t2) + 2, d.t.length);
      var gsSl = d.gs.slice(i1, i2), altSl = d.alt.slice(i1, i2), vsSl = d.vs.slice(i1, i2);
      var avg = gsSl.reduce(function (a, b) { return a + b; }, 0) / Math.max(gsSl.length, 1);
      var dist = sample(t2).dist - sample(t1).dist;
      return "SEGMENT " + fmtDur(t1) + "–" + fmtDur(t2) +
        "   ·   " + dist.toFixed(1) + " nm" +
        "   ·   GS avg " + Math.round(avg) + " / max " + Math.round(Math.max.apply(null, gsSl)) + " kt" +
        "   ·   ALT " + fmtInt(Math.min.apply(null, altSl)) + "–" + fmtInt(Math.max.apply(null, altSl)) + " ft" +
        "   ·   " + fmtInt(Math.max.apply(null, vsSl)) + " / " + fmtInt(Math.min.apply(null, vsSl)) + " fpm";
    },
    exportTrack: function (fmt) {
      /* Build a KML or GPX document from the loaded arrays and download it. */
      if (!d) return;
      var chip = document.getElementById("file-label");
      var base = ((chip && chip.textContent) || "flight").split(" ·")[0]
        .replace(/\.[^.]+$/, "").replace(/[^\w.-]+/g, "_") || "flight";
      var i, body;
      if (fmt === "gpx") {
        var pts = [];
        for (i = 0; i < d.t.length; i++) {
          var tAttr = d.meta.t0_ms != null
            ? "<time>" + new Date(d.meta.t0_ms + d.t[i] * 1000).toISOString() + "</time>" : "";
          pts.push('<trkpt lat="' + d.lat[i] + '" lon="' + d.lon[i] + '">' +
                   "<ele>" + (d.alt[i] / 3.280839895).toFixed(1) + "</ele>" + tAttr + "</trkpt>");
        }
        body = '<?xml version="1.0" encoding="UTF-8"?>\n' +
          '<gpx version="1.1" creator="flight-path-tracker" xmlns="http://www.topografix.com/GPX/1/1">' +
          "<trk><name>" + base + "</name><trkseg>" + pts.join("") + "</trkseg></trk></gpx>";
      } else {
        var coords = [];
        for (i = 0; i < d.t.length; i++) {
          coords.push(d.lon[i] + "," + d.lat[i] + "," + (d.alt[i] / 3.280839895).toFixed(1));
        }
        body = '<?xml version="1.0" encoding="UTF-8"?>\n' +
          '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>' + base + "</name>" +
          '<Placemark><name>Track</name><LineString><tessellate>1</tessellate>' +
          "<altitudeMode>absolute</altitudeMode><coordinates>" + coords.join(" ") +
          "</coordinates></LineString></Placemark></Document></kml>";
      }
      var blob = new Blob([body], {type: "application/xml"});
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = base + "." + fmt;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
    },
    prepUpload: function (contents, filename, maxRows) {
      /* Decode a dcc.Upload data-URI and, for very large files, stride-
       * decimate the rows in the browser so only a few MB ever reach the
       * server. The app never draws more than the display budgets anyway,
       * so visualization quality is unaffected. */
      if (!contents) return null;
      var name = filename || "uploaded.csv";
      var text;
      try {
        text = atob(contents.split(",", 2)[1]);
      } catch (e) {
        return {name: name, err: "could not decode the uploaded file"};
      }
      // Frame-delimited KLV dumps must be decimated by FRAME, not by line --
      // a line stride would tear frames apart and corrupt the format.
      var frameDelim = text.slice(0, 8192).match(/=+\s*FRAME\s*=+/);
      if (frameDelim) {
        var blocks = text.split(frameDelim[0]);
        var head = blocks.shift();                  // anything before frame 1
        var origFrames = blocks.length;
        if (origFrames > maxRows) {
          var fstride = Math.ceil(origFrames / maxRows);
          var kept = [];
          for (var b = 0; b < blocks.length; b += fstride) kept.push(blocks[b]);
          text = head + frameDelim[0] + kept.join(frameDelim[0]);
          return {name: name, csv: text, orig_rows: origFrames, kept_rows: kept.length};
        }
        return {name: name, csv: text, orig_rows: origFrames, kept_rows: origFrames};
      }
      var lines = text.split(/\r?\n/);
      while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
      var origRows = Math.max(0, lines.length - 1);
      if (lines.length > maxRows + 1) {
        var out = [lines[0]];                       // header (or first row)
        var stride = Math.ceil((lines.length - 1) / maxRows);
        for (var i = 1; i < lines.length; i += stride) {
          if (lines[i]) out.push(lines[i]);
        }
        var last = lines[lines.length - 1];
        if (last && out[out.length - 1] !== last) out.push(last);
        text = out.join("\n");
        return {name: name, csv: text, orig_rows: origRows, kept_rows: out.length - 1};
      }
      return {name: name, csv: lines.join("\n"), orig_rows: origRows, kept_rows: origRows};
    },
  };
})();
