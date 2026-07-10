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

  // 3D aircraft model (custom WebGL layer) + chase-camera state.
  var ac3d = {layer: null, gl: null, prog: null, posBuf: null, nrmBuf: null,
              count: 0, loc: {}, pose: null, ok: false};
  var chase = {t0: 0, blend: 1, raf: null, elev: 0};

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
  // Inline web-mercator math: the bundled MapLibre exposes no maplibregl
  // global, so MercatorCoordinate conversions are reimplemented here.
  // Mercator space: x,y in [0,1] across the world (y grows SOUTH), z uses
  // the same scale as x/y at a given latitude.
  var EARTH_R = 6378137;
  var EARTH_C = 2 * Math.PI * EARTH_R;
  var DEG = Math.PI / 180;
  function mercX(lon) { return (lon + 180) / 360; }
  function mercY(lat) {
    return (180 - 180 / Math.PI *
            Math.log(Math.tan(Math.PI / 4 + lat * DEG / 2))) / 360;
  }
  function mercPerMeter(lat) { return 1 / (EARTH_C * Math.cos(lat * DEG)); }
  function offsetLL(lat, lon, eastM, northM) {
    return {lat: lat + northM / (Math.PI * EARTH_R / 180),
            lng: lon + eastM / (Math.PI * EARTH_R / 180 * Math.cos(lat * DEG))};
  }
  function mul4(a, b) {   // column-major mat4 multiply, out = a * b
    var out = new Array(16);
    for (var c = 0; c < 4; c++) {
      for (var r = 0; r < 4; r++) {
        out[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] +
                         a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
      }
    }
    return out;
  }
  function exagOf(m) {
    var t = m.getTerrain && m.getTerrain();
    return (t && t.exaggeration) || 1.0;
  }
  function groundElev(m, lng, lat) {
    // Absolute rendered (exaggerated) meters. This MapLibre build's
    // queryTerrainElevation is relative to the map-center elevation, so
    // prefer the terrain object and compensate on the fallback path.
    try {
      if (m.terrain && m.terrain.getElevationForLngLatZoom) {
        // Clamp to the DEM source maxzoom (15) or the query warns + returns 0.
        return m.terrain.getElevationForLngLatZoom(
          {lng: lng, lat: lat}, Math.min(m.transform.tileZoom || 0, 15)) || 0;
      }
      var q = m.queryTerrainElevation && m.queryTerrainElevation([lng, lat]);
      if (q != null) return q + (m.transform.elevation || 0);
    } catch (e) {}
    return 0;
  }

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
  var CORE_LAYERS = ["ft-halo", "ft-ac", "ft-ac-fb", "ft-ac3d"];
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

  // ---- 3D aircraft model (raw-WebGL custom layer) --------------------------
  // Local model frame: +X right wing, +Y nose, +Z up; fuselage = 10 units.
  function buildPlaneMesh() {
    var P = [], N = [];
    function tri(a, b, c) {
      var ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2];
      var vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
      var nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
      var l = Math.hypot(nx, ny, nz) || 1;
      nx /= l; ny /= l; nz /= l;
      [a, b, c].forEach(function (p) {
        P.push(p[0], p[1], p[2]);
        N.push(nx, ny, nz);
      });
    }
    function quad(a, b, c, d) { tri(a, b, c); tri(a, c, d); }
    var NOSE = [0, 5, 0.1], TAIL = [0, -5, 0.35];
    var A1 = [-0.55, 3.2, 0.6], A2 = [0.55, 3.2, 0.6];
    var A3 = [0.55, 3.2, -0.4], A4 = [-0.55, 3.2, -0.4];
    var B1 = [-0.55, -2.8, 0.6], B2 = [0.55, -2.8, 0.6];
    var B3 = [0.55, -2.8, -0.4], B4 = [-0.55, -2.8, -0.4];
    tri(NOSE, A1, A2); tri(NOSE, A2, A3); tri(NOSE, A3, A4); tri(NOSE, A4, A1);
    quad(A1, A2, B2, B1);  // cabin top
    quad(A2, A3, B3, B2);  // cabin right
    quad(A3, A4, B4, B3);  // cabin bottom
    quad(A4, A1, B1, B4);  // cabin left
    tri(B1, B2, TAIL); tri(B2, B3, TAIL); tri(B3, B4, TAIL); tri(B4, B1, TAIL);
    // Swept wings with slight dihedral.
    quad([0.55, 1.6, -0.05], [5.2, -0.6, 0.25], [5.2, -1.2, 0.25], [0.55, 0.1, -0.05]);
    quad([-0.55, 1.6, -0.05], [-5.2, -0.6, 0.25], [-5.2, -1.2, 0.25], [-0.55, 0.1, -0.05]);
    // Horizontal stabilizers.
    quad([0.3, -4.2, 0.1], [1.9, -4.9, 0.18], [1.9, -5.2, 0.18], [0.3, -4.9, 0.1]);
    quad([-0.3, -4.2, 0.1], [-1.9, -4.9, 0.18], [-1.9, -5.2, 0.18], [-0.3, -4.9, 0.1]);
    // Vertical stabilizer.
    quad([0, -3.4, 0.3], [0, -4.4, 2.0], [0, -5.1, 2.0], [0, -5.0, 0.3]);
    return {pos: new Float32Array(P), nrm: new Float32Array(N), count: P.length / 3};
  }

  // Lighting stays in the pre-mirror ENU frame (u_rot, det +1): the
  // model→mercator matrix negates y, which would flip normals otherwise.
  // abs(dot) keeps thin single-sided surfaces lit from both sides.
  var AC3D_VS =
    "attribute vec3 a_pos;" +
    "attribute vec3 a_nrm;" +
    "uniform mat4 u_mvp;" +
    "uniform mat3 u_rot;" +
    "varying float v_shade;" +
    "void main() {" +
    "  gl_Position = u_mvp * vec4(a_pos, 1.0);" +
    "  vec3 n = normalize(u_rot * a_nrm);" +
    "  v_shade = 0.35 + 0.65 * abs(dot(n, normalize(vec3(0.4, 0.3, 0.85))));" +
    "}";
  var AC3D_FS =
    "precision mediump float;" +
    "varying float v_shade;" +
    "void main() { gl_FragColor = vec4(vec3(0.91, 0.94, 0.98) * v_shade, 1.0); }";

  function ac3dFree(gl) {
    if (gl) {
      try {
        if (ac3d.prog) gl.deleteProgram(ac3d.prog);
        if (ac3d.posBuf) gl.deleteBuffer(ac3d.posBuf);
        if (ac3d.nrmBuf) gl.deleteBuffer(ac3d.nrmBuf);
      } catch (e) {}
    }
    ac3d.prog = null;
    ac3d.posBuf = null;
    ac3d.nrmBuf = null;
    ac3d.gl = null;
    ac3d.ok = false;
  }

  function ac3dInit(gl) {
    ac3dFree(ac3d.gl);   // style reloads can hand us a fresh context
    try {
      var mk = function (type, src) {
        var sh = gl.createShader(type);
        gl.shaderSource(sh, src);
        gl.compileShader(sh);
        if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
          throw new Error(gl.getShaderInfoLog(sh));
        }
        return sh;
      };
      var prog = gl.createProgram();
      gl.attachShader(prog, mk(gl.VERTEX_SHADER, AC3D_VS));
      gl.attachShader(prog, mk(gl.FRAGMENT_SHADER, AC3D_FS));
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        throw new Error(gl.getProgramInfoLog(prog));
      }
      var mesh = buildPlaneMesh();
      var posBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.pos, gl.STATIC_DRAW);
      var nrmBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, nrmBuf);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.nrm, gl.STATIC_DRAW);
      ac3d.gl = gl;
      ac3d.prog = prog;
      ac3d.posBuf = posBuf;
      ac3d.nrmBuf = nrmBuf;
      ac3d.count = mesh.count;
      ac3d.loc = {
        pos: gl.getAttribLocation(prog, "a_pos"),
        nrm: gl.getAttribLocation(prog, "a_nrm"),
        mvp: gl.getUniformLocation(prog, "u_mvp"),
        rot: gl.getUniformLocation(prog, "u_rot"),
      };
      ac3d.ok = true;
    } catch (e) {
      ac3d.ok = false;
    }
  }

  function ac3dLayer() {
    return {
      id: "ft-ac3d", type: "custom", renderingMode: "3d",
      onAdd: function (map, gl) { ac3dInit(gl); },
      onRemove: function (map, gl) { ac3dFree(gl); },
      render: function (gl, matrix) {
        if (matrix && matrix.defaultProjectionData) {   // maplibre v5 shape
          matrix = matrix.defaultProjectionData.mainMatrix;
        }
        if (!threeD || !ac3d.ok || !ac3d.pose || !matrix) return;
        if (gl.bindVertexArray) gl.bindVertexArray(null);
        gl.useProgram(ac3d.prog);
        gl.enable(gl.DEPTH_TEST);
        gl.depthFunc(gl.LEQUAL);
        gl.disable(gl.CULL_FACE);
        gl.bindBuffer(gl.ARRAY_BUFFER, ac3d.posBuf);
        gl.enableVertexAttribArray(ac3d.loc.pos);
        gl.vertexAttribPointer(ac3d.loc.pos, 3, gl.FLOAT, false, 0, 0);
        gl.bindBuffer(gl.ARRAY_BUFFER, ac3d.nrmBuf);
        gl.enableVertexAttribArray(ac3d.loc.nrm);
        gl.vertexAttribPointer(ac3d.loc.nrm, 3, gl.FLOAT, false, 0, 0);
        gl.uniformMatrix4fv(ac3d.loc.mvp, false,
                            new Float32Array(mul4(matrix, ac3d.pose.model)));
        gl.uniformMatrix3fv(ac3d.loc.rot, false, new Float32Array(ac3d.pose.rot));
        gl.drawArrays(gl.TRIANGLES, 0, ac3d.count);
      },
    };
  }

  function camPlaneDistM(m, s) {
    // Real camera→aircraft distance in meters (drives constant-screen-size
    // scaling). cameraToCenterDistance is in pixels; convert via the
    // meters-per-pixel at the map center.
    var tr = m.transform, c = m.getCenter();
    var pitch = m.getPitch() * DEG, br = m.getBearing() * DEG;
    var mppC = Math.cos(c.lat * DEG) * EARTH_C /
               (tr.tileSize * Math.pow(2, m.getZoom()));
    var dc = tr.cameraToCenterDistance * mppC;
    var camE = -dc * Math.sin(pitch) * Math.sin(br);
    var camN = -dc * Math.sin(pitch) * Math.cos(br);
    var camU = (tr.elevation || 0) + dc * Math.cos(pitch);
    var pE = (s.lon - c.lng) * (Math.PI * EARTH_R / 180) * Math.cos(c.lat * DEG);
    var pN = (s.lat - c.lat) * (Math.PI * EARTH_R / 180);
    var pU = s.alt * 0.3048 * exagOf(m);
    return Math.max(50, Math.hypot(pE - camE, pN - camN, pU - camU));
  }

  function poseMatrices(m, s) {
    var exag = exagOf(m), k = mercPerMeter(s.lat);
    var x0 = mercX(s.lon), y0 = mercY(s.lat);
    var z0 = s.alt * 0.3048 * exag * k;
    var tr = m.transform;
    var fov = (tr && tr._fov) || 0.6435011087932844;
    var height = (tr && tr.height) || 600;
    var mpp = camPlaneDistM(m, s) * 2 * Math.tan(fov / 2) / height;
    var lenM = Math.max(30, ((d && d.meta.model_px) || 64) * mpp);
    var sc = (lenM / 10) * k;
    var h = s.hdg * DEG, p = (s.pitch || 0) * DEG, r = (s.roll || 0) * DEG;
    var sh = Math.sin(h), ch = Math.cos(h), sp = Math.sin(p), cp = Math.cos(p),
        sr = Math.sin(r), cr = Math.cos(r);
    // ENU basis (x east, y north, z up): yaw cw-from-north, then pitch
    // nose-up, then roll right-wing-down positive.
    var f = [sh * cp, ch * cp, sp];
    var r0 = [ch, -sh, 0], u0 = [-sh * sp, -ch * sp, cp];
    var rt = [r0[0] * cr - u0[0] * sr, r0[1] * cr - u0[1] * sr, r0[2] * cr - u0[2] * sr];
    var up = [u0[0] * cr + r0[0] * sr, u0[1] * cr + r0[1] * sr, u0[2] * cr + r0[2] * sr];
    // Column-major model matrix; north maps to mercator −y.
    return {
      model: [sc * rt[0], -sc * rt[1], sc * rt[2], 0,
              sc * f[0], -sc * f[1], sc * f[2], 0,
              sc * up[0], -sc * up[1], sc * up[2], 0,
              x0, y0, z0, 1],
      rot: [rt[0], rt[1], rt[2], f[0], f[1], f[2], up[0], up[1], up[2]],
    };
  }

  function syncAcVisibility(m) {
    // In 3D the model replaces the flat icon + halo; if the custom layer
    // failed to initialize, 3D keeps the flat symbol (graceful degradation).
    var vis = (threeD && ac3d.ok) ? "none" : "visible";
    ["ft-halo", "ft-ac", "ft-ac-fb"].forEach(function (id) {
      try {
        if (m.getLayer(id) && m.getLayoutProperty(id, "visibility") !== vis) {
          m.setLayoutProperty(id, "visibility", vis);
        }
      } catch (e) {}
    });
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
    // 3D model custom layer, isolated so a GL failure here can never take
    // down the symbol/fallback path above.
    try {
      if (!m.getLayer("ft-ac3d")) {
        if (!ac3d.layer) ac3d.layer = ac3dLayer();
        m.addLayer(ac3d.layer);
      }
    } catch (e) { ac3d.ok = false; }
    syncAcVisibility(m);
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
        chaseCamera(m, s);
      } else {
        // The per-frame jumpTo cancels any easeTo, so a leftover 3D pitch
        // or bearing would stick forever — decay them back to flat here.
        var o = {center: [s.lon, s.lat]};
        var p = m.getPitch(), b = m.getBearing();
        if (p > 0.1 || Math.abs(b) > 0.1) {
          o.pitch = p > 0.1 ? p * 0.85 : 0;
          o.bearing = b + (((-b + 540) % 360) - 180) * 0.15;
        }
        m.jumpTo(o);
      }
      syncCam(m);
    }
    if (threeD && ac3d.ok) {
      ac3d.pose = poseMatrices(m, s);
      m.triggerRepaint();
    }
  }

  // ---- chase camera ---------------------------------------------------------
  // True chase cam: the camera flies BACK meters behind and UP meters above
  // the aircraft at its real (exaggerated) altitude, aimed so the aircraft
  // sits a fixed AIM degrees below screen center with terrain/horizon ahead.
  // Expressed via calculateCameraOptionsFromTo -> ordinary jumpTo params, so
  // syncCam and plotly's layout stay in step for free.
  function chaseCamera(m, s) {
    var mm = d.meta;
    var BACK = mm.chase_back_m || 900, UP = mm.chase_above_m || 260;
    var AIM = (mm.chase_aim_deg || 7) * DEG, damp = mm.chase_damp || 0.12;
    // Damp the bearing toward the aircraft heading so the world banks
    // smoothly through turns instead of jittering; when paused (scrubbing)
    // snap exactly behind the aircraft instead.
    if (playing) {
      var diff = ((s.hdg - camBearing + 540) % 360) - 180;
      camBearing = (camBearing + diff * damp + 360) % 360;
    } else {
      camBearing = s.hdg;
    }
    var br = camBearing * DEG;
    var altR = s.alt * 0.3048 * exagOf(m);
    var cam = offsetLL(s.lat, s.lon, -BACK * Math.sin(br), -BACK * Math.cos(br));
    var camAlt = altR + UP;
    // Aim the center ray AIM degrees above the aircraft's ray so the plane
    // sits below screen center with terrain and horizon filling the frame.
    var dep = Math.max(Math.atan2(UP, BACK) - AIM, 3 * DEG);
    chase.elev += (groundElev(m, s.lon, s.lat) - chase.elev) * 0.15;
    var run = Math.max(200, (camAlt - chase.elev) / Math.tan(dep));
    var tgt = offsetLL(cam.lat, cam.lng, run * Math.sin(br), run * Math.cos(br));
    var opts;
    try {
      opts = m.calculateCameraOptionsFromTo(
        {lng: cam.lng, lat: cam.lat}, camAlt,
        {lng: tgt.lng, lat: tgt.lat}, groundElev(m, tgt.lng, tgt.lat));
    } catch (e) { return; }
    opts.bearing = camBearing;   // exact, avoids atan2 rounding wobble
    m.jumpTo(applyChaseBlend(m, opts));
  }

  function applyChaseBlend(m, opts) {
    // Smooth 700 ms entry into the chase framing. A plain easeTo would be
    // cancelled by the next frame's jumpTo, so blend per-frame instead.
    var k = Math.min(1, (performance.now() - chase.t0) / 700);
    chase.blend = k;
    if (k >= 1) return opts;
    k = k * k * (3 - 2 * k);
    var c = m.getCenter();
    var db = ((opts.bearing - m.getBearing() + 540) % 360) - 180;
    return {
      center: [c.lng + (opts.center.lng - c.lng) * k,
               c.lat + (opts.center.lat - c.lat) * k],
      zoom: m.getZoom() + (opts.zoom - m.getZoom()) * k,
      pitch: m.getPitch() + (opts.pitch - m.getPitch()) * k,
      bearing: m.getBearing() + db * k,
    };
  }

  function chaseEnter() {
    if (!d) return;
    camBearing = sample(curTime).hdg;
    chase.t0 = performance.now();
    chase.blend = 0;
    chase.elev = 0;
    if (!playing && !chase.raf) {
      // Paused: drive the entry blend with our own frame loop.
      var loop = function () {
        chase.raf = null;
        if (!d || !follow || !threeD || playing) return;
        render(sample(curTime));
        if (chase.blend < 1) chase.raf = window.requestAnimationFrame(loop);
      };
      loop();
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
        var m = mapObj();
        if (m) {
          if (threeD) {
            chaseEnter();
          } else {
            var s0 = sample(curTime);
            m.jumpTo({center: [s0.lon, s0.lat], zoom: d.meta.follow_zoom});
          }
        }
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
        ac3d.pose = null;
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
        if (threeD) {
          chaseEnter();
        } else {
          var s = sample(curTime);
          m.easeTo({center: [s.lon, s.lat], zoom: d.meta.follow_zoom,
                    pitch: 0, bearing: 0, duration: 700});
          syncCam(m);
        }
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
        // The ~76° chase pitch exceeds the default maxPitch of 60.
        try {
          if (m.setMaxPitch && (!m.getMaxPitch || m.getMaxPitch() < 80)) {
            m.setMaxPitch(80);
          }
        } catch (e) {}
        if (follow && d) {
          chaseEnter();
        } else {
          m.easeTo({pitch: 55, duration: 700});
        }
      } else {
        clearTerrain(m);
        m.easeTo({pitch: 0, bearing: 0, duration: 700});
      }
      syncAcVisibility(m);
      setTimeout(function () {
        syncCam(m);
        // Restore the default clamp only after pitch eased back to 0.
        if (!threeD) { try { m.setMaxPitch(60); } catch (e) {} }
      }, 750);
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
