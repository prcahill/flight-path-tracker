/* Client-side engine for the Flight Path Tracker.
 *
 * Owns everything per-flight: a roster of up to MAXF loaded flights, all map
 * rendering (native MapLibre layers — the plotly figure is just the basemap
 * shell), 60 fps playback of the ACTIVE flight, KPI/readout DOM, profiles,
 * exports and segment analytics. The server only parses files into flight
 * packages; switching flights is instant and requires no network.
 *
 * Visual model per the design: the active flight renders at full brightness
 * with the altitude-colored overlay, aircraft icon and sensor stare-point;
 * other roster flights are dimmed altitude-colored previews.
 */
"use strict";

window.FT = (function () {
  var MAXF = 4;
  var flights = [];      // flight packages (see app.py _flight_package)
  var active = -1;
  var d = null;          // alias of flights[active]
  var seq = 0;           // uid source for layer names

  var playing = false;
  var speed = 25;
  var follow = false;
  var threeD = false;
  var camBearing = 0;    // damped chase-camera bearing, degrees
  var curTime = 0;
  var anchorWall = 0;
  var anchorData = 0;
  var rafId = null;
  var lastSync = 0;
  var lastSyncValue = null;

  // ---- dom / plotly helpers ---------------------------------------------
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
  function setText(id, txt) {
    var el = document.getElementById(id);
    if (el) el.textContent = txt;
  }
  function setDisplay(id, show) {
    var el = document.getElementById(id);
    if (el) el.style.display = show ? "" : "none";
  }
  function setProps(id, props) {
    if (window.dash_clientside && window.dash_clientside.set_props) {
      window.dash_clientside.set_props(id, props);
    }
  }

  // ---- math helpers -------------------------------------------------------
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
    var lerpOpt = function (a) {
      if (!a || a[i] == null || a[j] == null) return null;
      return a[i] + (a[j] - a[i]) * w;
    };
    var dh = ((d.hdg[j] - d.hdg[i] + 540) % 360) - 180;
    return {
      t: t, lat: lerp(d.lat), lon: lerp(d.lon), alt: lerp(d.alt),
      gs: lerp(d.gs), vs: lerp(d.vs), dist: lerp(d.dist),
      hdg: ((d.hdg[i] + dh * w) % 360 + 360) % 360,
      pitch: lerpOpt(d.pitch), roll: lerpOpt(d.roll), slant: lerpOpt(d.slant),
      fcLat: lerpOpt(d.fc_lat), fcLon: lerpOpt(d.fc_lon),
    };
  }

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec));
    var p = function (n) { return String(n).padStart(2, "0"); };
    return p(Math.floor(sec / 3600)) + ":" + p(Math.floor((sec % 3600) / 60)) + ":" + p(sec % 60);
  }
  var COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
  function fmtInt(x) { return Math.round(x).toLocaleString("en-US"); }
  function strideIdx(n, budget) {
    var out = [];
    var step = Math.max(1, Math.ceil(n / budget));
    for (var i = 0; i < n; i += step) out.push(i);
    if (out[out.length - 1] !== n - 1) out.push(n - 1);
    return out;
  }
  function pick(arr, idx) { return idx.map(function (i) { return arr[i]; }); }

  // ---- geojson builders ---------------------------------------------------
  var EMPTY_FC = {type: "FeatureCollection", features: []};
  function pointFC(lon, lat, props) {
    return {type: "FeatureCollection", features: [{type: "Feature", id: 1,
            geometry: {type: "Point", coordinates: [lon, lat]},
            properties: props || {}}]};
  }
  function lineFC(pkg) {
    var coords = [];
    for (var i = 0; i < pkg.map.line_lat.length; i++) {
      coords.push([pkg.map.line_lon[i], pkg.map.line_lat[i]]);
    }
    return {type: "FeatureCollection", features: [{type: "Feature",
            geometry: {type: "LineString", coordinates: coords}, properties: {}}]};
  }
  function marksFC(pkg) {
    var feats = [];
    for (var i = 0; i < pkg.map.mk_lat.length; i++) {
      feats.push({type: "Feature",
        geometry: {type: "Point", coordinates: [pkg.map.mk_lon[i], pkg.map.mk_lat[i]]},
        properties: {c: pkg.map.mk_color[i]}});
    }
    return {type: "FeatureCollection", features: feats};
  }

  // ---- map layer management ----------------------------------------------
  var CORE_LAYERS = ["ft-halo", "ft-ac", "ft-ac-fb"];
  var planeOk = false;

  function planeImage() {
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

  function ensureFlightLayers(m, pkg) {
    var lineSrc = "ft-f" + pkg.uid + "-line";
    var markSrc = "ft-f" + pkg.uid + "-marks";
    if (!m.getSource(lineSrc)) {
      m.addSource(lineSrc, {type: "geojson", data: lineFC(pkg)});
    }
    if (!m.getSource(markSrc)) {
      m.addSource(markSrc, {type: "geojson", data: marksFC(pkg)});
    }
    if (!m.getLayer(lineSrc)) {
      m.addLayer({id: lineSrc, type: "line", source: lineSrc,
        paint: {"line-color": "#FFFFFF", "line-width": 1.2, "line-opacity": 0.1}});
    }
    if (!m.getLayer(markSrc)) {
      m.addLayer({id: markSrc, type: "circle", source: markSrc,
        paint: {"circle-color": ["get", "c"], "circle-radius": 3,
                "circle-opacity": 0.5, "circle-stroke-width": 0}});
    }
  }

  function ensureCoreLayers(m) {
    if (!m.getSource("ft-halo-src")) {
      m.addSource("ft-halo-src", {type: "geojson", data: EMPTY_FC});
    }
    if (!m.getLayer("ft-halo")) {
      m.addLayer({id: "ft-halo", type: "circle", source: "ft-halo-src",
        paint: {"circle-radius": 13, "circle-color": "rgba(44,123,229,0.38)"}});
    }
    if (!m.getSource("ft-ac-src")) {
      m.addSource("ft-ac-src", {type: "geojson", data: EMPTY_FC});
    }
    planeOk = false;
    try {
      if (!m.hasImage("ft-plane")) m.addImage("ft-plane", planeImage(), {pixelRatio: 2});
      if (!m.getLayer("ft-ac")) {
        m.addLayer({id: "ft-ac", type: "symbol", source: "ft-ac-src",
          layout: {"icon-image": "ft-plane", "icon-size": 0.72,
                   "icon-rotate": ["get", "hdg"], "icon-rotation-alignment": "map",
                   "icon-allow-overlap": true, "icon-ignore-placement": true}});
      }
      planeOk = true;
    } catch (e) {
      if (!m.getLayer("ft-ac-fb")) {
        m.addLayer({id: "ft-ac-fb", type: "circle", source: "ft-ac-src",
          paint: {"circle-radius": 5.5, "circle-color": "#FFFFFF"}});
      }
    }
  }

  function applyEmphasis(m) {
    flights.forEach(function (pkg, i) {
      var on = i === active;
      var lineId = "ft-f" + pkg.uid + "-line";
      var markId = "ft-f" + pkg.uid + "-marks";
      if (m.getLayer(lineId)) {
        m.setPaintProperty(lineId, "line-opacity", on ? 0.22 : 0.06);
      }
      if (m.getLayer(markId)) {
        m.setPaintProperty(markId, "circle-opacity", on ? 0.95 : 0.25);
        m.setPaintProperty(markId, "circle-radius", on ? 3.4 : 2.4);
      }
    });
  }

  function bumpCore(m) {
    CORE_LAYERS.forEach(function (id) {
      if (m.getLayer(id)) { try { m.moveLayer(id); } catch (e) {} }
    });
  }

  function rebuildAll() {
    var m = mapObj();
    if (!m) return false;
    if (!m.__ftStyleHook) {
      m.__ftStyleHook = true;
      m.on("style.load", function () { rebuildAll(); });
    }
    if (!m.isStyleLoaded()) return false;
    try {
      flights.forEach(function (pkg) { ensureFlightLayers(m, pkg); });
      ensureCoreLayers(m);
      bumpCore(m);
      applyEmphasis(m);
      // Enforce the 3D state on every rebuild: basemap changes drop terrain,
      // and a 3D-off during a style transition can leave it stuck on.
      if (threeD) ensureTerrain(m);
      else if (m.getTerrain()) clearTerrain(m);
      return true;
    } catch (e) { return false; }
  }

  // ---- 3D terrain -----------------------------------------------------------
  // Free, keyless AWS Open Data terrain tiles (Mapzen terrarium encoding).
  var DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";

  function ensureTerrain(m) {
    try {
      if (!m.getSource("ft-dem")) {
        m.addSource("ft-dem", {type: "raster-dem", tiles: [DEM_URL],
          encoding: "terrarium", tileSize: 256, maxzoom: 15,
          attribution: "Terrain: Mapzen/AWS Open Data"});
      }
      if (!m.getTerrain()) m.setTerrain({source: "ft-dem", exaggeration: 1.3});
      return true;
    } catch (e) { return false; }
  }

  function clearTerrain(m) {
    try { m.setTerrain(null); } catch (e) {}
  }

  function syncCam(m) {
    // Keep plotly's stored layout in step with the real camera so a later
    // figure-level pass cannot snap pitch/bearing/center back.
    var g = gd();
    if (!g || !g.layout.map) return;
    var c = m.getCenter();
    var cam = {lat: c.lat, lon: c.lng};
    g.layout.map.center = cam;
    g.layout.map.zoom = m.getZoom();
    g.layout.map.pitch = m.getPitch();
    g.layout.map.bearing = m.getBearing();
    g._fullLayout.map.center = cam;
    g._fullLayout.map.zoom = m.getZoom();
    g._fullLayout.map.pitch = m.getPitch();
    g._fullLayout.map.bearing = m.getBearing();
  }

  function dropFlightLayers(m, pkg) {
    ["-line", "-marks"].forEach(function (kind) {
      var id = "ft-f" + pkg.uid + kind;
      try {
        if (m.getLayer(id)) m.removeLayer(id);
        if (m.getSource(id)) m.removeSource(id);
      } catch (e) {}
    });
  }

  // ---- rendering -----------------------------------------------------------
  function drawMarker(s) {
    var m = mapObj();
    if (!m) return;
    rebuildAll();
    try {
      m.getSource("ft-halo-src").setData(pointFC(s.lon, s.lat));
      m.getSource("ft-ac-src").setData(pointFC(s.lon, s.lat, {hdg: s.hdg}));
    } catch (e) { /* map still initializing */ }
    if (follow) {
      if (threeD) {
        // Chase cam: damp the bearing toward the aircraft heading so the
        // world banks smoothly through turns instead of jittering.
        var diff = ((s.hdg - camBearing + 540) % 360) - 180;
        camBearing = (camBearing + diff * 0.12 + 360) % 360;
        m.jumpTo({center: [s.lon, s.lat], bearing: camBearing});
      } else {
        m.jumpTo({center: [s.lon, s.lat]});
      }
      syncCam(m);
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
    } catch (e) {}
  }

  function render(s, opts) {
    opts = opts || {};
    drawMarker(s);
    drawReadout(s);
    if (opts.cursors) drawCursors(s);
    if (opts.slider) {
      lastSyncValue = s.t;
      setProps("scrub", {value: s.t});
    }
  }

  // ---- roster / statics ----------------------------------------------------
  function renderRoster() {
    var host = document.getElementById("roster");
    if (!host) return;
    host.textContent = "";
    flights.forEach(function (pkg, i) {
      var chip = document.createElement("div");
      chip.className = "fchip" + (i === active ? " on" : "");
      chip.title = pkg.label;
      var tag = document.createElement("span");
      tag.className = "fchip-tag";
      tag.textContent = "F" + (i + 1);
      var nm = document.createElement("span");
      nm.className = "fchip-name";
      nm.textContent = pkg.name;
      var x = document.createElement("span");
      x.className = "fchip-x";
      x.textContent = "×";
      x.title = "Remove this flight";
      x.onclick = function (e) { e.stopPropagation(); api.removeFlight(i); };
      chip.onclick = function () { api.setActive(i); };
      chip.appendChild(tag);
      chip.appendChild(nm);
      chip.appendChild(x);
      host.appendChild(chip);
    });
  }

  function axisRefs(pg, row) {
    // Axis references of the two base traces built by the server shell.
    var t = pg.data[row === 1 ? 0 : 2];
    return {xaxis: t.xaxis || "x", yaxis: t.yaxis || "y"};
  }

  function writeProfiles() {
    var pg = profilesGd();
    if (!pg || !pg.data || pg.data.length < 4) return;
    try {
      // Remove any preview traces (index > 3), then rebuild.
      if (pg.data.length > 4) {
        var del = [];
        for (var k = 4; k < pg.data.length; k++) del.push(k);
        window.Plotly.deleteTraces(pg, del);
      }
      if (!d) {
        window.Plotly.restyle(pg, {x: [[], [], [], []], y: [[], [], [], []]}, [0, 1, 2, 3]);
        return;
      }
      var idx = strideIdx(d.t.length, 4000);
      var tm = idx.map(function (i) { return d.t[i] / 60; });
      window.Plotly.restyle(pg, {
        x: [tm, [0], tm, [0]],
        y: [pick(d.alt, idx), [d.alt[0]], pick(d.gs, idx), [d.gs[0]]],
      }, [0, 1, 2, 3]);
      // Dimmed previews of the other flights.
      var a1 = axisRefs(pg, 1), a2 = axisRefs(pg, 2);
      var newTraces = [];
      flights.forEach(function (pkg, i) {
        if (i === active) return;
        var pidx = strideIdx(pkg.t.length, 1500);
        var ptm = pidx.map(function (j) { return pkg.t[j] / 60; });
        newTraces.push({x: ptm, y: pick(pkg.alt, pidx), mode: "lines",
          line: {color: "rgba(90,169,255,0.25)", width: 1},
          hoverinfo: "skip", xaxis: a1.xaxis, yaxis: a1.yaxis});
        newTraces.push({x: ptm, y: pick(pkg.gs, pidx), mode: "lines",
          line: {color: "rgba(143,227,136,0.25)", width: 1},
          hoverinfo: "skip", xaxis: a2.xaxis, yaxis: a2.yaxis});
      });
      if (newTraces.length) window.Plotly.addTraces(pg, newTraces);
    } catch (e) {}
  }

  function writeStatics() {
    if (!d) {
      for (var i = 0; i < 8; i++) setText("kpi-v-" + i, "—");
      ["rd-time","rd-pos","rd-alt","rd-gs","rd-hdg","rd-vs","rd-dist"].forEach(
        function (id) { setText(id, "—"); });
      setDisplay("rrow-pitch", false);
      setDisplay("rrow-roll", false);
      setDisplay("rrow-slant", false);
      setDisplay("alt-legend", false);
      setText("clock", "00:00:00 / 00:00:00");
      setText("seg-stats", "");
      writeProfiles();
      return;
    }
    d.kpis.forEach(function (v, i) { setText("kpi-v-" + i, v); });
    setDisplay("rrow-pitch", !!d.pitch);
    setDisplay("rrow-roll", !!d.roll);
    setDisplay("rrow-slant", !!d.slant);
    setDisplay("alt-legend", true);
    setText("leg-max", fmtInt(d.meta.alt_max));
    setText("leg-min", fmtInt(d.meta.alt_min));
    setText("seg-stats", "");
    writeProfiles();
  }

  function fitActive() {
    var m = mapObj();
    if (!m || !d) return;
    m.jumpTo({center: [d.meta.center_lon, d.meta.center_lat],
              zoom: d.meta.fit_zoom, bearing: 0,
              pitch: threeD ? 55 : 0});
    syncCam(m);
  }

  // ---- playback loop --------------------------------------------------------
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

  function stopPlayback() {
    playing = false;
    if (rafId) { window.cancelAnimationFrame(rafId); rafId = null; }
    setProps("play", {children: "▶  PLAY"});
  }

  // ---- public api ------------------------------------------------------------
  var api = {
    load: function (pkg) {
      if (!pkg) return;
      if (flights.length >= MAXF) {
        setText("file-label", "⚠ roster full (" + MAXF + ") — remove a flight first");
        return;
      }
      pkg.uid = ++seq;
      flights.push(pkg);
      api.setActive(flights.length - 1);
    },
    setActive: function (i) {
      if (i < 0 || i >= flights.length) return;
      stopPlayback();
      active = i;
      d = flights[i];
      curTime = Math.min(curTime, d.meta.duration_s);
      renderRoster();
      writeStatics();
      lastSyncValue = curTime;
      setProps("scrub", {max: d.meta.duration_s,
                         step: Math.max(0.1, d.meta.duration_s / 2000),
                         value: curTime});
      rebuildAll();
      if (follow) {
        var s0 = sample(curTime);
        var m = mapObj();
        if (m) m.jumpTo({center: [s0.lon, s0.lat], zoom: d.meta.follow_zoom});
      } else {
        fitActive();
      }
      render(sample(curTime), {cursors: true});
      // The plotly graphs may still be mounting on first page load; retry
      // the graph-dependent rendering until they exist.
      var self = this;
      var attempts = 0;
      var lateInit = function () {
        attempts += 1;
        var mapReady = !!mapObj() && rebuildAll();
        var profReady = !!profilesGd() && profilesGd().data;
        if (profReady) writeProfiles();
        if (d && mapReady) {
          if (!follow) fitActive();
          render(sample(curTime), {cursors: !!profReady});
        }
        if ((!mapReady || !profReady) && attempts < 25) setTimeout(lateInit, 400);
      };
      setTimeout(lateInit, 300);
    },
    removeFlight: function (i) {
      if (i < 0 || i >= flights.length) return;
      var m = mapObj();
      if (m) dropFlightLayers(m, flights[i]);
      flights.splice(i, 1);
      if (flights.length === 0) {
        stopPlayback();
        active = -1;
        d = null;
        curTime = 0;
        renderRoster();
        writeStatics();
        lastSyncValue = 0;
        setProps("scrub", {max: 1, value: 0});
        if (m) {
          try {
            m.getSource("ft-halo-src").setData(EMPTY_FC);
            m.getSource("ft-ac-src").setData(EMPTY_FC);
          } catch (e) {}
        }
        return;
      }
      if (i === active) {
        active = -1;               // force full reactivation
        curTime = 0;
        api.setActive(Math.min(i, flights.length - 1));
      } else {
        if (i < active) active -= 1;
        renderRoster();
        if (m) applyEmphasis(m);
        writeProfiles();
      }
    },
    count: function () { return flights.length; },
    togglePlay: function () {
      if (!d) return "▶  PLAY";
      if (playing) {
        playing = false;
        return "▶  PLAY";
      }
      if (curTime >= d.meta.duration_s) curTime = 0;
      playing = true;
      anchor();
      lastSync = 0;
      if (!rafId) rafId = window.requestAnimationFrame(tick);
      return "❚❚  PAUSE";
    },
    setSpeed: function (mult) {
      anchor();
      speed = mult || 1;
    },
    seek: function (t) {
      if (!d) return;
      var raw = +t || 0;
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
      if (!m || !d) return;
      if (follow) {
        var s = sample(curTime);
        camBearing = s.hdg;
        m.easeTo({center: [s.lon, s.lat],
                  zoom: d.meta.follow_zoom + (threeD ? 2 : 0),
                  pitch: threeD ? 60 : 0,
                  bearing: threeD ? s.hdg : 0,
                  duration: 700});
        syncCam(m);
      } else {
        fitActive();
      }
    },
    set3D: function (on) {
      threeD = !!on;
      var m = mapObj();
      if (!m) return;
      if (threeD) {
        ensureTerrain(m);
        var opts = {pitch: follow ? 60 : 55, duration: 700};
        if (follow && d) {
          var s = sample(curTime);
          camBearing = s.hdg;
          opts.bearing = s.hdg;
          opts.zoom = d.meta.follow_zoom + 2;
          opts.center = [s.lon, s.lat];
        }
        m.easeTo(opts);
      } else {
        clearTerrain(m);
        m.easeTo({pitch: 0, bearing: 0, duration: 700});
      }
      setTimeout(function () { syncCam(m); }, 750);
    },
    setBasemap: function (style) {
      var g = gd();
      if (g) window.Plotly.relayout(g, {"map.style": style});
      // style.load hook re-adds all flight + core layers
    },
    segmentStats: function (sel) {
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
      if (!d) return;
      var base = (d.name || "flight").replace(/\.[^.]+$/, "").replace(/[^\w.-]+/g, "_") || "flight";
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
      if (!contents) return null;
      var name = filename || "uploaded.csv";
      if (flights.length >= MAXF) {
        return {name: name, err: "roster full (" + MAXF + ") — remove a flight first"};
      }
      var text;
      try {
        text = atob(contents.split(",", 2)[1]);
      } catch (e) {
        return {name: name, err: "could not decode the uploaded file"};
      }
      var frameDelim = text.slice(0, 8192).match(/=+\s*FRAME\s*=+/);
      if (frameDelim) {
        var blocks = text.split(frameDelim[0]);
        var head = blocks.shift();
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
        var out = [lines[0]];
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
  return api;
})();
