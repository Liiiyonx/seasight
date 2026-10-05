/**
 * 三维作业仿真场：海面 / 浮动平台 / 六轴机械臂。
 *
 * 这一层只负责渲染与动画，不持有任何业务规则：
 *  - 平台位置来自真实 telemetry（home / target / position 经纬度 → 场景坐标）
 *  - 机械臂姿态由仿真 phase + progress 驱动，不自己推进任务状态
 *  - paused 时冻结作业时钟，只保留海面、灯光等环境动效
 *
 * 因此将来把仿真换成真机遥测，这个场景可以原样复用。
 */

import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js'

const TRACK_LENGTH = 14
const SEA_SIZE = 150
const SEA_SEGMENTS = 140
const DEBRIS_COUNT = 72
const ROUTE_SEGMENTS = 72

const PALETTE = {
  hull: 0x36434e,
  hullDark: 0x232d35,
  deck: 0x4a5763,
  graphite: 0x3d4954,
  graphiteDark: 0x232b32,
  metal: 0xc2cbd2,
  metalDark: 0x8d979f,
  accent: 0x18a999,
  accentBright: 0x33e3c9,
  warning: 0xffb020,
  glass: 0x9adbe8,
  foam: 0xff9500,
  plastic: 0x0a84ff,
  mixed: 0xff3b30,
  seaDeep: 0x03161f,
  seaShallow: 0x0e5566,
  horizonLight: 0xc6dfec,
  horizonDark: 0x0a1f2a,
  skyTopLight: 0x5d9fd4,
  skyBottomLight: 0xd8e9f1,
  skyTopDark: 0x061a26,
  skyBottomDark: 0x143c4a,
}

const DEG = Math.PI / 180

/** 终态工单的轨迹回放一圈所需秒数（1x 倍速）。 */
const REPLAY_SECONDS = 22

/** 回放时间轴分段：导航 / 清理 / 返航，与后端 14s / 8s / 11s 节拍同比例。 */
const REPLAY_NAV_END = 0.46
const REPLAY_COLLECT_END = 0.72

/** 与海面顶点着色器保持同一波形，用于平台/漂浮物的随浪起伏。 */
function waveHeight(x, z, time) {
  const ly = -z
  return (
    Math.sin(x * 0.34 + time * 1.05) * 0.22 +
    Math.sin(ly * 0.27 - time * 0.82) * 0.17 +
    Math.sin((x + ly) * 0.19 + time * 1.55) * 0.1
  )
}

function clamp(value, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value))
}

function smoothstep(edge0, edge1, x) {
  const t = clamp((x - edge0) / (edge1 - edge0))
  return t * t * (3 - 2 * t)
}

/** 关键帧插值：stops = [[进度, [角度...]], ...]，返回插值后的角度数组。 */
function sampleStops(stops, x) {
  const first = stops[0]
  const last = stops[stops.length - 1]
  if (x <= first[0]) return first[1].slice()
  if (x >= last[0]) return last[1].slice()
  for (let i = 0; i < stops.length - 1; i += 1) {
    const a = stops[i]
    const b = stops[i + 1]
    if (x <= b[0]) {
      const local = smoothstep(a[0], b[0], x)
      return a[1].map((value, index) => value + (b[1][index] - value) * local)
    }
  }
  return last[1].slice()
}

function softTexture() {
  const size = 128
  const canvas = document.createElement('canvas')
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
  gradient.addColorStop(0, 'rgba(255,255,255,0.95)')
  gradient.addColorStop(0.35, 'rgba(255,255,255,0.42)')
  gradient.addColorStop(1, 'rgba(255,255,255,0)')
  ctx.fillStyle = gradient
  ctx.fillRect(0, 0, size, size)
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  return texture
}

const STOW_POSE = [0, -0.5, 1.8, 0, 0.65, 0]
const SCAN_POSE = [0, -0.86, 1.34, 0, 0.42, 0]
const PARK_POSE = [0, -0.32, 1.96, 0, 0.82, 0]
const ERROR_POSE = [0, -0.18, 2.04, 0.2, 0.9, 0.1]

/** 一个完整抓取循环：下探到舷外水面 → 沿舷侧扫向船艏 → 提升 → 回转到料仓 → 投放 → 归位。 */
const SCOOP_STOPS = [
  [0.0, [0.0, -0.5, 1.8, 0.0, 0.65, 0.0]],
  [0.15, [-1.15, -0.06, -1.48, 0.0, -1.32, 0.0]],
  [0.28, [-1.57, -0.9, -1.66, 0.0, -0.54, 0.0]],
  [0.4, [-1.0, -0.9, -1.66, 0.0, -0.54, 0.0]],
  [0.52, [-0.55, -0.9, -1.66, 0.0, -0.54, 0.0]],
  [0.6, [-0.3, -1.06, -1.86, 0.0, 0.64, 0.0]],
  [0.72, [-0.95, -0.06, -1.48, 0.0, -1.32, 0.0]],
  [0.84, [-0.3, -0.2, 2.0, 0.0, 0.1, 0.0]],
  [1.0, [0.0, -0.5, 1.8, 0.0, 0.65, 0.0]],
]

const JOINT_LABELS = ['J1', 'J2', 'J3', 'J4', 'J5', 'J6']

/**
 * 创建三维作业场。
 *
 * @param {object} options
 * @param {HTMLCanvasElement} options.canvas
 * @param {(angles: number[]) => void} [options.onJoints] 关节角变化回调（约 8Hz）
 * @param {(error: Error) => void} [options.onFail] WebGL 初始化失败回调
 */
export function createRobotArmScene({ canvas, onJoints, onFail }) {
  let renderer
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,
      powerPreference: 'high-performance',
    })
  } catch (error) {
    onFail?.(error)
    return null
  }
  if (!renderer.getContext()) {
    onFail?.(new Error('WebGL context unavailable'))
    return null
  }

  const disposables = []
  const track = (item) => {
    disposables.push(item)
    return item
  }

  const lowPower =
    window.matchMedia?.('(max-width: 900px)').matches ||
    (navigator.hardwareConcurrency || 8) <= 4

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, lowPower ? 1.5 : 2))
  renderer.setClearAlpha(0)
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.toneMapping = THREE.ACESFilmicToneMapping
  renderer.toneMappingExposure = 1.04
  renderer.shadowMap.enabled = !lowPower
  // ★ three r186 已移除 PCFSoftShadowMap，继续用会在控制台刷
  //   「THREE.WebGLShadowMap: PCFSoftShadowMap has been removed」告警。
  //   用 PCFShadowMap 代替，视觉差异可忽略，但控制台能保持干净。
  renderer.shadowMap.type = THREE.PCFShadowMap

  const scene = new THREE.Scene()
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 400)
  camera.position.set(7.6, 5.4, 8.4)

  const controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.075
  controls.rotateSpeed = 0.72
  controls.zoomSpeed = 0.8
  controls.panSpeed = 0.6
  controls.minDistance = 3.2
  controls.maxDistance = 34
  controls.minPolarAngle = 0.18
  controls.maxPolarAngle = 1.47
  controls.target.set(0, 1, 0)

  // ---------------------------------------------------------------- 灯光
  const hemi = new THREE.HemisphereLight(0xd6ecf8, 0x10222b, 1.15)
  scene.add(hemi)

  const sun = new THREE.DirectionalLight(0xfff4e2, 2.4)
  sun.position.set(7, 10, 6)
  sun.castShadow = !lowPower
  if (sun.shadow) {
    sun.shadow.mapSize.set(2048, 2048)
    sun.shadow.camera.near = 1
    sun.shadow.camera.far = 42
    sun.shadow.camera.left = -11
    sun.shadow.camera.right = 11
    sun.shadow.camera.top = 11
    sun.shadow.camera.bottom = -11
    sun.shadow.bias = -0.0009
    sun.shadow.normalBias = 0.022
  }
  scene.add(sun)
  scene.add(sun.target)

  const fill = new THREE.DirectionalLight(0x9fd8ea, 0.55)
  fill.position.set(-8, 6, -5)
  scene.add(fill)

  // ---------------------------------------------------------------- 海面
  const seaUniforms = {
    uTime: { value: 0 },
    uSun: { value: new THREE.Vector3(0.5, 0.78, 0.38).normalize() },
    uDeep: { value: new THREE.Color(PALETTE.seaDeep) },
    uShallow: { value: new THREE.Color(PALETTE.seaShallow) },
    uHorizon: { value: new THREE.Color(PALETTE.horizonLight) },
  }

  const seaMaterial = track(
    new THREE.ShaderMaterial({
      uniforms: seaUniforms,
      vertexShader: /* glsl */ `
        uniform float uTime;
        varying vec3 vWorld;
        varying vec3 vNormalW;
        varying float vHeight;
        varying float vDist;

        float wave(vec2 p, float t) {
          return sin(p.x * 0.34 + t * 1.05) * 0.16
               + sin(p.y * 0.27 - t * 0.82) * 0.13
               + sin((p.x + p.y) * 0.19 + t * 1.55) * 0.07;
        }

        void main() {
          vec3 p = position;
          float h = wave(p.xy, uTime);
          p.z += h;

          float eps = 0.42;
          float hl = wave(p.xy - vec2(eps, 0.0), uTime);
          float hr = wave(p.xy + vec2(eps, 0.0), uTime);
          float hd = wave(p.xy - vec2(0.0, eps), uTime);
          float hu = wave(p.xy + vec2(0.0, eps), uTime);
          vec3 n = normalize(vec3(
            -(hr - hl) / (2.0 * eps) * 1.6,
            -(hu - hd) / (2.0 * eps) * 1.6,
            1.0
          ));

          vec4 world = modelMatrix * vec4(p, 1.0);
          vWorld = world.xyz;
          vNormalW = normalize(mat3(modelMatrix) * n);
          vHeight = h;
          vDist = length((viewMatrix * world).xyz);
          gl_Position = projectionMatrix * viewMatrix * world;
        }
      `,
      fragmentShader: /* glsl */ `
        uniform vec3 uSun;
        uniform vec3 uDeep;
        uniform vec3 uShallow;
        uniform vec3 uHorizon;
        varying vec3 vWorld;
        varying vec3 vNormalW;
        varying float vHeight;
        varying float vDist;

        void main() {
          vec3 N = normalize(vNormalW);
          vec3 V = normalize(cameraPosition - vWorld);
          vec3 L = normalize(uSun);
          vec3 H = normalize(L + V);

          float diff = clamp(dot(N, L), 0.0, 1.0);
          float spec = pow(max(dot(N, H), 0.0), 130.0);
          float rim = pow(1.0 - clamp(dot(N, V), 0.0, 1.0), 3.0);

          vec3 color = mix(uDeep, uShallow, smoothstep(-0.24, 0.26, vHeight * 2.2));
          color += diff * 0.1;
          color += vec3(0.86, 0.95, 1.0) * spec * 0.8;
          color = mix(color, vec3(0.5, 0.68, 0.72), smoothstep(0.17, 0.31, vHeight) * 0.3);
          color += rim * 0.05;

          float fog = smoothstep(20.0, 82.0, vDist);
          gl_FragColor = vec4(mix(color, uHorizon, fog), 1.0);

          #include <tonemapping_fragment>
          #include <colorspace_fragment>
        }
      `,
    }),
  )

  const sea = new THREE.Mesh(new THREE.PlaneGeometry(SEA_SIZE, SEA_SIZE, SEA_SEGMENTS, SEA_SEGMENTS), seaMaterial)
  sea.rotation.x = -Math.PI / 2
  sea.receiveShadow = !lowPower
  track(sea.geometry)
  scene.add(sea)

  // ---------------------------------------------------------------- 天空穹顶
  const skyUniforms = {
    uTop: { value: new THREE.Color(PALETTE.skyTopLight) },
    uBottom: { value: new THREE.Color(PALETTE.skyBottomLight) },
    uHorizon: { value: new THREE.Color(PALETTE.horizonLight) },
    uSunDir: { value: new THREE.Vector3(0.5, 0.78, 0.38).normalize() },
  }
  const skyMaterial = track(
    new THREE.ShaderMaterial({
      uniforms: skyUniforms,
      side: THREE.BackSide,
      depthWrite: false,
      vertexShader: /* glsl */ `
        varying vec3 vDir;
        void main() {
          vDir = normalize(position);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: /* glsl */ `
        uniform vec3 uTop;
        uniform vec3 uBottom;
        uniform vec3 uHorizon;
        uniform vec3 uSunDir;
        varying vec3 vDir;

        float hash(vec2 p) {
          return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
        }

        void main() {
          vec3 dir = normalize(vDir);
          float h = clamp(dir.y, -0.08, 1.0);
          float t = pow(h, 0.62);
          vec3 sky = mix(uHorizon, uBottom, smoothstep(-0.08, 0.08, dir.y));
          sky = mix(sky, uTop, pow(max(h, 0.0), 0.72));

          float sun = pow(max(dot(dir, uSunDir), 0.0), 260.0) * 1.6;
          float glow = pow(max(dot(dir, uSunDir), 0.0), 14.0) * 0.34;
          sky += vec3(1.0, 0.92, 0.72) * (sun + glow);

          vec2 cells = floor(dir.xz * 14.0 + dir.y * 5.0);
          float cloud = smoothstep(0.82, 0.97, hash(cells + 0.1));
          sky = mix(sky, vec3(1.0, 1.0, 1.0), cloud * 0.12 * smoothstep(0.05, 0.45, dir.y));

          gl_FragColor = vec4(sky, 1.0);
          #include <tonemapping_fragment>
          #include <colorspace_fragment>
        }
      `,
    }),
  )
  const skyDome = new THREE.Mesh(
    track(new THREE.SphereGeometry(190, 48, 24)),
    skyMaterial,
  )
  skyDome.frustumCulled = false
  skyDome.renderOrder = -10
  scene.add(skyDome)

  // ------------------------------------------------------- 材质 / 几何工具
  const gloss = (color, options = {}) =>
    track(
      new THREE.MeshStandardMaterial({
        color,
        roughness: options.roughness ?? 0.5,
        metalness: options.metalness ?? 0.35,
        emissive: options.emissive ?? 0x000000,
        emissiveIntensity: options.emissiveIntensity ?? 1,
        transparent: options.transparent ?? false,
        opacity: options.opacity ?? 1,
        side: options.side ?? THREE.FrontSide,
      }),
    )

  const matHull = gloss(PALETTE.hull, { roughness: 0.55, metalness: 0.3 })
  const matHullDark = gloss(PALETTE.hullDark, { roughness: 0.68, metalness: 0.25 })
  const matDeck = gloss(PALETTE.deck, { roughness: 0.62, metalness: 0.22 })
  const matGraphite = gloss(PALETTE.graphite, { roughness: 0.42, metalness: 0.5 })
  const matGraphiteDark = gloss(PALETTE.graphiteDark, { roughness: 0.46, metalness: 0.55 })
  const matMetal = gloss(PALETTE.metal, { roughness: 0.28, metalness: 0.85 })
  const matMetalDark = gloss(PALETTE.metalDark, { roughness: 0.35, metalness: 0.8 })
  const matAccent = gloss(PALETTE.accent, { roughness: 0.34, metalness: 0.3 })
  const matWarning = gloss(PALETTE.warning, { roughness: 0.4, metalness: 0.25 })
  const matGlass = gloss(PALETTE.glass, {
    roughness: 0.12,
    metalness: 0.05,
    transparent: true,
    opacity: 0.28,
    side: THREE.DoubleSide,
  })
  const matFoam = gloss(PALETTE.foam, { roughness: 0.6, metalness: 0.05 })
  const matPlastic = gloss(PALETTE.plastic, { roughness: 0.5, metalness: 0.1 })
  const matMixed = gloss(PALETTE.mixed, { roughness: 0.6, metalness: 0.05 })

  const geoCache = new Map()
  const box = (w, h, d, radius) => {
    const key = `b${w}|${h}|${d}|${radius ?? ''}`
    if (!geoCache.has(key)) {
      const radiusSafe = Math.min(radius ?? Math.min(w, h, d) * 0.12, Math.min(w, h, d) * 0.48)
      geoCache.set(key, new RoundedBoxGeometry(w, h, d, 3, radiusSafe))
    }
    return geoCache.get(key)
  }
  const cyl = (rt, rb, h, seg = 20, open = false) => {
    const key = `c${rt}|${rb}|${h}|${seg}|${open}`
    if (!geoCache.has(key)) geoCache.set(key, new THREE.CylinderGeometry(rt, rb, h, seg, 1, open))
    return geoCache.get(key)
  }
  const sphere = (r, seg = 18) => {
    const key = `s${r}|${seg}`
    if (!geoCache.has(key)) geoCache.set(key, new THREE.SphereGeometry(r, seg, Math.max(8, seg / 2)))
    return geoCache.get(key)
  }

  const mesh = (geometry, material, options = {}) => {
    const item = new THREE.Mesh(geometry, material)
    item.castShadow = options.cast ?? !lowPower
    item.receiveShadow = options.receive ?? !lowPower
    return item
  }

  const glowTexture = track(softTexture())
  const glowSprite = (color, size, opacity = 0.85) => {
    const material = track(
      new THREE.SpriteMaterial({
        map: glowTexture,
        color,
        transparent: true,
        opacity,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    )
    const sprite = new THREE.Sprite(material)
    sprite.scale.set(size, size, 1)
    return sprite
  }

  // ------------------------------------------------------------ 坐标换算
  // 场景坐标：以 home 为原点，按 1 单位 ≈ TRACK_LENGTH/实际距离 缩放，
  // 再整体平移使 home/target 关于原点对称。这样任何量级的现场都能满屏展示。
  const homeVec = new THREE.Vector3()
  const targetVec = new THREE.Vector3()
  const axis = new THREE.Vector3(0, 0, -1)
  const sideVec = new THREE.Vector3(1, 0, 0)
  let metersPerUnit = 1
  let hasTrack = false
  let refLat = 26.35
  let refLng = 119.86

  function projectMeters(lng, lat) {
    const scaleLng = 111320 * Math.cos((refLat * Math.PI) / 180)
    return new THREE.Vector3((lng - refLng) * scaleLng, 0, -(lat - refLat) * 110540)
  }

  function sceneFromLngLat(lng, lat) {
    const meters = projectMeters(lng, lat)
    const point = meters.multiplyScalar(1 / metersPerUnit)
    point.x += (homeVec.x + targetVec.x) / 2
    point.z += (homeVec.z + targetVec.z) / 2
    return point
  }

  function updateTrack(home, target) {
    if (!home || !target) return false
    const nextRefLat = home.lat
    const nextRefLng = home.lng
    if (
      hasTrack &&
      nextRefLat === refLat &&
      nextRefLng === refLng &&
      target.lat === targetVec.userData?.lat &&
      target.lng === targetVec.userData?.lng
    ) {
      return false
    }
    refLat = nextRefLat
    refLng = nextRefLng

    const targetMeters = projectMeters(target.lng, target.lat)
    const distance = targetMeters.length()
    if (distance < 0.5) {
      // 起终点几乎重合，用默认朝向撑开一段可视航程
      targetMeters.set(0, 0, -TRACK_LENGTH)
    }
    metersPerUnit = Math.max(distance, 0.5) / TRACK_LENGTH

    homeVec.copy(sceneFromLngLat(home.lng, home.lat))
    targetVec.copy(sceneFromLngLat(target.lng, target.lat))
    axis.copy(targetVec).sub(homeVec).setY(0)
    if (axis.lengthSq() < 1e-6) axis.set(0, 0, -1)
    axis.normalize()
    sideVec.set(axis.z, 0, -axis.x).normalize()

    targetVec.userData = { lat: target.lat, lng: target.lng }
    hasTrack = true
    return true
  }

  function fitTrackToRoute(route, home = null, target = null) {
    if (!route || route.length < 2) return
    const points = route
      .map((point) => {
        const lng = Number(point.lng)
        const lat = Number(point.lat)
        return Number.isFinite(lng) && Number.isFinite(lat) ? { lng, lat } : null
      })
      .filter(Boolean)
    if (points.length < 2) return

    const parsePoint = (point) => {
      if (!point) return null
      const lng = Number(point.lng)
      const lat = Number(point.lat)
      return Number.isFinite(lng) && Number.isFinite(lat) ? { lng, lat } : null
    }
    // 轨迹可能是 home → target → home 的闭合航线，首尾不能代表作业目标点。
    // 回放阶段优先使用接口返回的真实 home/target 作为场景基准。
    const homePoint = parsePoint(home) || points[0]
    const targetPoint = parsePoint(target) || points[points.length - 1]

    const lngs = points.map((point) => point.lng)
    const lats = points.map((point) => point.lat)
    const minLng = Math.min(...lngs)
    const maxLng = Math.max(...lngs)
    const minLat = Math.min(...lats)
    const maxLat = Math.max(...lats)
    refLat = homePoint.lat
    refLng = homePoint.lng
    const extent = new THREE.Vector3(
      (maxLng - minLng) * 111320 * Math.cos((refLat * Math.PI) / 180),
      0,
      -(maxLat - minLat) * 110540,
    ).length()
    if (extent < 0.5) return

    metersPerUnit = extent / TRACK_LENGTH
    homeVec.set(0, 0, 0)
    targetVec.set(0, 0, 0)
    homeVec.copy(sceneFromLngLat(homePoint.lng, homePoint.lat))
    targetVec.copy(sceneFromLngLat(targetPoint.lng, targetPoint.lat))
    axis.copy(targetVec).sub(homeVec).setY(0)
    if (axis.lengthSq() < 1e-6) axis.set(0, 0, -1)
    axis.normalize()
    sideVec.set(axis.z, 0, -axis.x).normalize()
    state.mountAngle = Math.atan2(axis.x, axis.z)
    targetVec.userData = { lat: targetPoint.lat, lng: targetPoint.lng }
    applyTrack()
  }

  // ------------------------------------------------------- 岸端 / 目标浮标
  const homeGroup = new THREE.Group()
  scene.add(homeGroup)
  {
    const deck = mesh(box(2.6, 0.18, 1.5, 0.05), matDeck)
    deck.position.y = 0.22
    homeGroup.add(deck)

    for (const [x, z] of [
      [-1.05, 0.55],
      [1.05, 0.55],
      [-1.05, -0.55],
      [1.05, -0.55],
    ]) {
      const pile = mesh(cyl(0.09, 0.09, 0.9, 10), matHullDark)
      pile.position.set(x, -0.2, z)
      homeGroup.add(pile)
    }

    const cabin = mesh(box(0.8, 0.55, 0.7, 0.06), matGraphite)
    cabin.position.set(-0.75, 0.58, 0)
    homeGroup.add(cabin)

    const roof = mesh(box(0.94, 0.07, 0.84, 0.03), matAccent)
    roof.position.set(-0.75, 0.88, 0)
    homeGroup.add(roof)

    const mast = mesh(cyl(0.05, 0.05, 1.5, 8), matMetalDark)
    mast.position.set(0.95, 1.05, 0.35)
    homeGroup.add(mast)

    const lamp = mesh(cyl(0.16, 0.22, 0.16, 12), matGraphiteDark)
    lamp.position.set(0.95, 1.86, 0.35)
    homeGroup.add(lamp)

    const lampGlow = glowSprite(0xfff0c2, 0.9, 0.5)
    lampGlow.position.set(0.95, 1.9, 0.35)
    homeGroup.add(lampGlow)

    // 靠泊引导环：机器人起始点
    const ringMat = track(
      new THREE.MeshBasicMaterial({
        color: PALETTE.accent,
        transparent: true,
        opacity: 0.5,
        side: THREE.DoubleSide,
        depthWrite: false,
      }),
    )
    const ring = new THREE.Mesh(track(new THREE.RingGeometry(0.78, 0.92, 48)), ringMat)
    ring.rotation.x = -Math.PI / 2
    ring.position.y = 0.04
    homeGroup.add(ring)
  }

  const targetGroup = new THREE.Group()
  scene.add(targetGroup)
  let targetRingMaterial = null
  let targetRing = null
  {
    const buoy = mesh(cyl(0.3, 0.2, 0.42, 16), matWarning)
    buoy.position.y = 0.16
    targetGroup.add(buoy)

    const stripe = mesh(cyl(0.305, 0.305, 0.1, 16), matGraphiteDark)
    stripe.position.y = 0.22
    targetGroup.add(stripe)

    const pole = mesh(cyl(0.035, 0.035, 1.25, 8), matMetal)
    pole.position.y = 0.85
    targetGroup.add(pole)

    const flag = mesh(box(0.42, 0.26, 0.02, 0.01), matWarning)
    flag.position.set(0.22, 1.3, 0)
    targetGroup.add(flag)

    const beacon = mesh(sphere(0.08, 14), matWarning, { cast: false })
    beacon.position.y = 1.5
    targetGroup.add(beacon)
    const beaconGlow = glowSprite(PALETTE.warning, 1.5, 0.7)
    beaconGlow.position.y = 1.5
    targetGroup.add(beaconGlow)

    targetRingMaterial = track(
      new THREE.MeshBasicMaterial({
        color: PALETTE.warning,
        transparent: true,
        opacity: 0.55,
        side: THREE.DoubleSide,
        depthWrite: false,
      }),
    )
    targetRing = new THREE.Mesh(track(new THREE.RingGeometry(1.25, 1.42, 64)), targetRingMaterial)
    targetRing.rotation.x = -Math.PI / 2
    targetRing.position.y = 0.05
    targetGroup.add(targetRing)
  }

  // ------------------------------------------------------------ 漂浮垃圾带
  const debrisGeometry = track(box(0.2, 0.1, 0.16, 0.03))
  const debrisMaterial = track(
    new THREE.MeshStandardMaterial({ color: 0xdfe6e9, roughness: 0.85, metalness: 0.04 }),
  )
  const debris = new THREE.InstancedMesh(debrisGeometry, debrisMaterial, DEBRIS_COUNT)
  debris.instanceMatrix.setUsage(THREE.DynamicDrawUsage)
  debris.castShadow = false
  debris.receiveShadow = false
  scene.add(debris)

  // 漂浮垃圾收拢在船边可达弧区：右舷梁 → 船艏，机械臂下探轨迹覆盖这一带。
  const DEBRIS_WAYPOINTS = [
    [1.26, -0.28],
    [1.16, -0.5],
    [1.04, -0.72],
    [0.9, -0.9],
    [0.74, -1.05],
    [0.55, -1.18],
    [0.34, -1.3],
    [0.1, -1.4],
  ]
  const debrisSeeds = []
  for (let i = 0; i < DEBRIS_COUNT; i += 1) {
    const [baseX, baseZ] = DEBRIS_WAYPOINTS[i % DEBRIS_WAYPOINTS.length]
    const baseAngle = Math.atan2(baseX, baseZ)
    const baseRadius = Math.hypot(baseX, baseZ)
    const radius = baseRadius * (0.9 + Math.random() * 0.22)
    const angle = baseAngle + (Math.random() - 0.5) * 0.2
    debrisSeeds.push({
      x: Math.sin(angle) * radius,
      z: Math.cos(angle) * radius,
      spin: Math.random() * Math.PI * 2,
      tilt: (Math.random() - 0.5) * 0.55,
      scale: 0.45 + Math.random() * 0.9,
    })
  }

  const debrisColor = new THREE.Color()
  const debrisPalette = [PALETTE.foam, PALETTE.foam, 0xdfe6e9, PALETTE.plastic, PALETTE.mixed]
  for (let i = 0; i < DEBRIS_COUNT; i += 1) {
    debrisColor.setHex(debrisPalette[i % debrisPalette.length])
    debris.setColorAt(i, debrisColor)
  }
  if (debris.instanceColor) debris.instanceColor.needsUpdate = true

  // -------------------------------------------------------- 航线（已走/剩余）
  const routeRibbonGeometry = track(new THREE.BufferGeometry())
  const ribbonPositions = new Float32Array(ROUTE_SEGMENTS * 6 * 3)
  routeRibbonGeometry.setAttribute('position', new THREE.BufferAttribute(ribbonPositions, 3))
  const routeRibbonMaterial = track(
    new THREE.MeshBasicMaterial({
      color: PALETTE.accent,
      transparent: true,
      opacity: 0.82,
      side: THREE.DoubleSide,
      depthWrite: false,
    }),
  )
  const routeRibbon = new THREE.Mesh(routeRibbonGeometry, routeRibbonMaterial)
  routeRibbon.frustumCulled = false
  scene.add(routeRibbon)

  const dotGeometry = track(new THREE.CircleGeometry(0.09, 12))
  const dotMaterial = track(
    new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: 0.55,
      side: THREE.DoubleSide,
      depthWrite: false,
    }),
  )
  const routeDots = new THREE.InstancedMesh(dotGeometry, dotMaterial, 26)
  routeDots.instanceMatrix.setUsage(THREE.DynamicDrawUsage)
  routeDots.frustumCulled = false
  scene.add(routeDots)

  // ------------------------------------------------------------ 作业船
  const platform = new THREE.Group()
  scene.add(platform)

  const platformParts = new THREE.Group()
  platform.add(platformParts)

  {
    // 船体：尖艏、外飘舷侧、尾封板，由轮廓挤出而成
    const hullOutline = new THREE.Shape()
    hullOutline.moveTo(0, 1.52)
    hullOutline.lineTo(0.62, 1.16)
    hullOutline.quadraticCurveTo(0.86, 0.62, 0.74, 0.06)
    hullOutline.quadraticCurveTo(0.6, -0.5, 0.5, -0.88)
    hullOutline.lineTo(0.43, -1.3)
    hullOutline.quadraticCurveTo(0.1, -1.4, 0, -1.42)
    hullOutline.quadraticCurveTo(-0.1, -1.4, -0.43, -1.3)
    hullOutline.lineTo(-0.5, -0.88)
    hullOutline.quadraticCurveTo(-0.6, -0.5, -0.74, 0.06)
    hullOutline.quadraticCurveTo(-0.86, 0.62, -0.62, 1.16)
    hullOutline.quadraticCurveTo(-0.32, 1.36, 0, 1.52)
    const hullGeo = track(
      new THREE.ExtrudeGeometry(hullOutline, {
        depth: 0.34,
        bevelEnabled: true,
        bevelThickness: 0.05,
        bevelSize: 0.05,
        bevelSegments: 3,
        curveSegments: 18,
      }),
    )
    hullGeo.translate(0, 0, -0.17)
    hullGeo.rotateX(Math.PI / 2)
    const hull = new THREE.Mesh(hullGeo, matHull)
    hull.castShadow = !lowPower
    hull.receiveShadow = !lowPower
    platformParts.add(hull)

    // 舷侧深色护舷条
    const rubRail = mesh(box(0.36, 0.12, 2.78, 0.04), matHullDark)
    rubRail.position.set(0, 0.27, 0.02)
    platformParts.add(rubRail)

    // 首部甲板：外飘折线 + 浅色走道
    const deck = mesh(box(1.56, 0.1, 2.72, 0.04), matDeck)
    deck.position.y = 0.42
    platformParts.add(deck)

    const deckRail = mesh(box(1.68, 0.05, 0.1, 0.02), matAccent)
    deckRail.position.set(0, 0.48, -1.24)
    platformParts.add(deckRail)

    const bowRail = mesh(box(0.72, 0.05, 0.1, 0.02), matAccent)
    bowRail.rotation.y = 0.16
    bowRail.position.set(0, 0.48, -1.46)
    platformParts.add(bowRail)

    const deckTrimRear = mesh(box(1.68, 0.05, 0.1, 0.02), matAccent)
    deckTrimRear.position.set(0, 0.48, 1.28)
    platformParts.add(deckTrimRear)

    // 驾驶台：前甲板上的舰桥
    const cabin = mesh(box(1.08, 0.66, 0.82, 0.06), matGraphite)
    cabin.position.set(0, 0.8, -0.82)
    platformParts.add(cabin)

    const cabinRoof = mesh(box(1.22, 0.08, 0.94, 0.03), matHullDark)
    cabinRoof.position.set(0, 1.17, -0.82)
    platformParts.add(cabinRoof)

    const cabinGlass = mesh(box(1.0, 0.28, 0.04, 0.01), matGlass, { cast: false })
    cabinGlass.position.set(0, 0.9, -1.22)
    platformParts.add(cabinGlass)

    const cabinDoor = mesh(box(0.26, 0.38, 0.05, 0.01), matMetalDark)
    cabinDoor.position.set(0.42, 0.72, -1.24)
    platformParts.add(cabinDoor)

    // 驾驶台两侧的通风口
    for (const x of [-0.58, 0.58]) {
      const vent = mesh(cyl(0.07, 0.09, 0.2, 10), matMetalDark)
      vent.position.set(x, 0.58, -0.92)
      platformParts.add(vent)
    }

    // 船尾料仓：三根玻璃管对应泡沫 / 塑料 / 混合
    const binDefs = [
      { x: -0.52, color: PALETTE.foam, material: matFoam },
      { x: 0, color: PALETTE.plastic, material: matPlastic },
      { x: 0.52, color: PALETTE.mixed, material: matMixed },
    ]
    const hopper = mesh(box(1.5, 0.44, 0.6, 0.06), matGraphiteDark)
    hopper.position.set(0, 0.7, 0.9)
    platformParts.add(hopper)

    const hopperLip = mesh(box(1.54, 0.06, 0.64, 0.02), matAccent)
    hopperLip.position.set(0, 0.95, 0.9)
    platformParts.add(hopperLip)

    platform.userData.binFills = []
    for (const def of binDefs) {
      const tube = mesh(cyl(0.17, 0.17, 0.34, 18, true), matGlass, { cast: false })
      tube.position.set(def.x, 0.66, 1.22)
      platformParts.add(tube)

      const fill = mesh(cyl(0.145, 0.145, 0.3, 18), def.material)
      fill.position.set(def.x, 0.48, 1.22)
      fill.scale.y = 0.001
      fill.castShadow = false
      platformParts.add(fill)
      platform.userData.binFills.push(fill)
    }

    // 驾驶台顶桅杆 / 雷达 / 航标灯
    const mast = mesh(cyl(0.055, 0.07, 1.05, 10), matMetalDark)
    mast.position.set(0.58, 1.25, -0.82)
    platformParts.add(mast)

    const crossBar = mesh(box(0.5, 0.05, 0.05, 0.02), matMetalDark)
    crossBar.position.set(0.58, 1.62, -0.82)
    platformParts.add(crossBar)

    const radarPivot = new THREE.Group()
    radarPivot.position.set(0.58, 1.78, -0.82)
    platformParts.add(radarPivot)
    const dish = mesh(new THREE.SphereGeometry(0.2, 18, 10, 0, Math.PI), matMetal, {
      cast: true,
    })
    dish.rotation.x = -Math.PI / 2.6
    radarPivot.add(dish)
    track(dish.geometry)
    platform.userData.radar = radarPivot

    const beacon = mesh(sphere(0.07, 12), matAccent, { cast: false })
    beacon.position.set(-0.58, 1.58, -0.82)
    platformParts.add(beacon)
    const platformBeaconGlow = glowSprite(PALETTE.accentBright, 1.1, 0.75)
    platformBeaconGlow.position.copy(beacon.position)
    platformParts.add(platformBeaconGlow)
    platform.userData.beacon = beacon
    platform.userData.beaconGlow = platformBeaconGlow

    const navPort = mesh(sphere(0.055, 12), matMixed, { cast: false })
    navPort.position.set(-0.7, 0.5, -0.55)
    platformParts.add(navPort)
    const navStarboard = mesh(sphere(0.055, 12), matAccent, { cast: false })
    navStarboard.position.set(0.7, 0.5, -0.55)
    platformParts.add(navStarboard)

    // 尾流
    const wakeMaterial = track(
      new THREE.MeshBasicMaterial({
        map: glowTexture,
        color: 0xeaf7ff,
        transparent: true,
        opacity: 0,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    )
    const wake = new THREE.Mesh(track(new THREE.PlaneGeometry(1.1, 3.4)), wakeMaterial)
    wake.rotation.x = -Math.PI / 2
    wake.position.set(0, 0.06, 2.55)
    platform.add(wake)
    platform.userData.wake = wakeMaterial
  }

  // -------------------------------------------------------------- 机械臂
  const arm = new THREE.Group()
  arm.position.set(0, 0.52, -0.24)
  platform.add(arm)

  const joints = []
  const jointHandles = []

  {
    const basePlate = mesh(cyl(0.4, 0.46, 0.1, 24), matGraphiteDark)
    basePlate.position.y = 0.05
    arm.add(basePlate)

    const boltRing = new THREE.Group()
    for (let i = 0; i < 8; i += 1) {
      const bolt = mesh(cyl(0.032, 0.032, 0.05, 8), matMetal, { cast: false })
      bolt.position.set(Math.cos((i / 8) * Math.PI * 2) * 0.34, 0.11, Math.sin((i / 8) * Math.PI * 2) * 0.34)
      boltRing.add(bolt)
    }
    arm.add(boltRing)

    // J1 —— 回转
    const j1 = new THREE.Group()
    j1.position.y = 0.1
    arm.add(j1)
    joints.push(j1)

    const turret = mesh(cyl(0.3, 0.34, 0.26, 24), matGraphite)
    turret.position.y = 0.13
    j1.add(turret)

    const turretRing = mesh(cyl(0.315, 0.315, 0.05, 24), matAccent)
    turretRing.position.y = 0.24
    j1.add(turretRing)

    // J2 —— 大臂俯仰
    const j2 = new THREE.Group()
    j2.position.y = 0.3
    j1.add(j2)
    joints.push(j2)

    const shoulder = mesh(cyl(0.2, 0.2, 0.5, 20), matGraphiteDark)
    shoulder.rotation.z = Math.PI / 2
    j2.add(shoulder)

    const shoulderCapL = mesh(cyl(0.12, 0.12, 0.06, 16), matAccent)
    shoulderCapL.rotation.z = Math.PI / 2
    shoulderCapL.position.x = -0.26
    j2.add(shoulderCapL)
    const shoulderCapR = shoulderCapL.clone()
    shoulderCapR.position.x = 0.26
    j2.add(shoulderCapR)

    const upperLink = mesh(box(0.26, 0.72, 0.28, 0.08), matGraphite)
    upperLink.position.y = 0.36
    j2.add(upperLink)

    const upperStripe = mesh(box(0.28, 0.5, 0.05, 0.02), matAccent)
    upperStripe.position.set(0, 0.36, -0.15)
    j2.add(upperStripe)

    // J3 —— 小臂
    const j3 = new THREE.Group()
    j3.position.y = 0.7
    j2.add(j3)
    joints.push(j3)

    const elbow = mesh(cyl(0.17, 0.17, 0.42, 20), matGraphiteDark)
    elbow.rotation.z = Math.PI / 2
    j3.add(elbow)

    const elbowCap = mesh(cyl(0.1, 0.1, 0.05, 16), matAccent)
    elbowCap.rotation.z = Math.PI / 2
    elbowCap.position.x = -0.22
    j3.add(elbowCap)
    const elbowCap2 = elbowCap.clone()
    elbowCap2.position.x = 0.22
    j3.add(elbowCap2)

    const foreLink = mesh(box(0.2, 0.54, 0.22, 0.07), matGraphite)
    foreLink.position.y = 0.28
    j3.add(foreLink)

    const foreStripe = mesh(box(0.22, 0.36, 0.045, 0.02), matAccent)
    foreStripe.position.set(0, 0.28, -0.12)
    j3.add(foreStripe)

    // J4 —— 腕部回转
    const j4 = new THREE.Group()
    j4.position.y = 0.54
    j3.add(j4)
    joints.push(j4)

    const wristRoll = mesh(cyl(0.12, 0.12, 0.22, 18), matMetalDark)
    wristRoll.position.y = 0.1
    j4.add(wristRoll)

    // J5 —— 腕部俯仰
    const j5 = new THREE.Group()
    j5.position.y = 0.22
    j4.add(j5)
    joints.push(j5)

    const wristBend = mesh(cyl(0.13, 0.13, 0.28, 18), matGraphiteDark)
    wristBend.rotation.z = Math.PI / 2
    j5.add(wristBend)

    // J6 —— 末端回转 + 夹爪
    const j6 = new THREE.Group()
    j6.position.y = 0.18
    j5.add(j6)
    joints.push(j6)

    const flange = mesh(cyl(0.1, 0.1, 0.07, 18), matMetal)
    flange.position.y = 0.04
    j6.add(flange)

    const gripperBody = mesh(box(0.26, 0.12, 0.2, 0.04), matGraphite)
    gripperBody.position.y = 0.13
    j6.add(gripperBody)

    const fingerGeo = box(0.055, 0.24, 0.16, 0.02)
    const fingerL = mesh(fingerGeo, matMetal)
    fingerL.position.set(-0.1, 0.27, 0)
    j6.add(fingerL)
    const fingerR = mesh(fingerGeo, matMetal)
    fingerR.position.set(0.1, 0.27, 0)
    j6.add(fingerR)
    jointHandles.push({ fingers: [fingerL, fingerR], spread: 0.1 })

    const carried = mesh(box(0.17, 0.09, 0.14, 0.03), matFoam, { cast: false })
    carried.position.set(0, 0.3, 0)
    carried.visible = false
    j6.add(carried)
    arm.userData.carried = carried

    // 液压管：贴着大臂走，让机械感更真
    const hose = new THREE.CatmullRomCurve3([
      new THREE.Vector3(0.16, 0.04, 0.16),
      new THREE.Vector3(0.22, 0.34, 0.2),
      new THREE.Vector3(0.18, 0.66, 0.16),
    ])
    const hoseMesh = mesh(new THREE.TubeGeometry(hose, 18, 0.026, 8, false), matGraphiteDark, {
      cast: false,
    })
    j2.add(hoseMesh)
    track(hoseMesh.geometry)
  }

  // ---------------------------------------------------------------- 状态
  const state = {
    time: 0,
    missionTime: 0,
    follow: true,
    frozen: false,
    phase: 'ack',
    stateName: 'idle',
    speed: 1,
    progress: 0,
    heading: 0,
    targetHeading: 0,
    battery: 100,
    binTarget: [0, 0, 0],
    binCurrent: [0, 0, 0],
    pose: STOW_POSE.slice(),
    poseTarget: STOW_POSE.slice(),
    gripper: 1,
    gripperTarget: 1,
    platformPos: new THREE.Vector3(),
    platformTarget: new THREE.Vector3(),
    hasPosition: false,
    debrisHidden: 0,
    debrisHeading: 0,
    debrisOrigin: new THREE.Vector3(),
    debrisLocked: false,
    mountAngle: 0,
    replay: {
      active: false,
      playing: false,
      progress: 0,
      speed: 1,
      route: [],
      targetIndex: 0,
      phase: 'navigating',
      battery: 100,
      bins: { foam: 0, plastic: 0, mixed: 0 },
    },
  }

  const tmpVec = new THREE.Vector3()
  const tmpVec2 = new THREE.Vector3()
  const tmpMatrix = new THREE.Matrix4()
  const tmpQuat = new THREE.Quaternion()
  const tmpScale = new THREE.Vector3()

  function applyTrack() {
    homeGroup.position.copy(homeVec)
    homeGroup.rotation.y = -state.mountAngle
    targetGroup.position.copy(targetVec)
    targetGroup.rotation.y = -state.mountAngle

    if (!state.hasPosition) {
      state.platformPos.copy(homeVec)
      state.platformTarget.copy(homeVec)
    }
  }

  function applyTelemetry(telemetry = {}) {
    const changed = updateTrack(telemetry.home, telemetry.target)
    if (changed || !state.hasPosition) {
      state.hasPosition = true
      state.platformPos.copy(homeVec)
      state.platformTarget.copy(homeVec)
      state.pose = STOW_POSE.slice()
      state.poseTarget = STOW_POSE.slice()
    }
    if (changed) {
      state.mountAngle = Math.atan2(axis.x, axis.z)
      applyTrack()
    }

    state.phase = telemetry.phase || 'ack'
    state.stateName = telemetry.state || 'idle'
    state.speed = Number(telemetry.speed) || 1
    state.progress = clamp(Number(telemetry.progress) || 0)
    state.heading = Number(telemetry.heading) || 0
    state.targetHeading = -state.heading * DEG
    state.frozen = ['paused', 'done', 'stopped', 'error', 'history'].includes(state.stateName)

    if (Number.isFinite(Number(telemetry.battery))) {
      state.battery = Number(telemetry.battery)
    }
    const bins = telemetry.bins || {}
    state.binTarget = [
      clamp(Number(bins.foam) || 0),
      clamp(Number(bins.plastic) || 0),
      clamp(Number(bins.mixed) || 0),
    ]

    const position = telemetry.position
    if (position && Number.isFinite(Number(position.lng)) && Number.isFinite(Number(position.lat))) {
      state.platformTarget.copy(sceneFromLngLat(Number(position.lng), Number(position.lat)))
    }
  }

  const replayVec = new THREE.Vector3()
  const replayPoint = { lng: 0, lat: 0 }

  function nearestRouteIndex(route, target) {
    if (!target || route.length < 2) return Math.floor((route.length - 1) / 2)
    const lng = Number(target.lng)
    const lat = Number(target.lat)
    const cosLat = Math.cos((lat * Math.PI) / 180)
    let best = Math.floor((route.length - 1) / 2)
    let bestDistance = Infinity
    for (let i = 0; i < route.length; i += 1) {
      const dlng = (Number(route[i].lng) - lng) * 111320 * cosLat
      const dlat = (Number(route[i].lat) - lat) * 110540
      const distance = dlng * dlng + dlat * dlat
      if (distance < bestDistance) {
        bestDistance = distance
        best = i
      }
    }
    return best
  }

  function replayTelemetry(progress) {
    const route = state.replay.route
    if (!route.length) return
    const total = route.length - 1
    const targetIndex = Math.min(total, Math.max(0, Math.round(state.replay.targetIndex)))
    const targetFraction = total > 0 ? targetIndex / total : 0.5
    const t = clamp(progress)

    // 与后端真实节拍对齐：导航约 46%、清理约 26%、返航约 28% 的回放时长，
    // 否则清理窗口会被压缩到一两秒，机械臂整套抓取动作看不完整。
    const navEnd = REPLAY_NAV_END
    const collectEnd = REPLAY_COLLECT_END

    let phase = 'navigating'
    let routeFraction = 0
    let collect = 0
    if (t >= 0.985) {
      phase = 'done'
      routeFraction = 1
      collect = 1
    } else if (t >= collectEnd) {
      phase = 'returning'
      routeFraction =
        targetFraction + ((t - collectEnd) / (0.985 - collectEnd)) * (1 - targetFraction)
    } else if (t >= navEnd) {
      phase = 'collecting'
      routeFraction = targetFraction
      collect = clamp((t - navEnd) / (collectEnd - navEnd))
    } else {
      routeFraction = (t / navEnd) * targetFraction
    }

    if (phase === 'collecting') {
      // 作业时船停在目标点，机械臂在右舷海面捞取，不再边返航边作业。
      replayPoint.lng = Number(route[targetIndex].lng)
      replayPoint.lat = Number(route[targetIndex].lat)
    } else {
      const position = routeFraction * total
      const index = Math.min(total, Math.max(0, Math.floor(position)))
      const next = Math.min(total, index + 1)
      const local = position - index
      const a = route[index]
      const b = route[next]
      replayPoint.lng = Number(a.lng) + (Number(b.lng) - Number(a.lng)) * local
      replayPoint.lat = Number(a.lat) + (Number(b.lat) - Number(a.lat)) * local
    }

    replayVec.copy(sceneFromLngLat(replayPoint.lng, replayPoint.lat))
    if (phase !== 'collecting') {
      const previous = state.platformPos
      const dx = replayVec.x - previous.x
      const dz = replayVec.z - previous.z
      // 只在真的有位移时更新航向，停船作业时保持进泊姿态而不是原地打转。
      if (dx * dx + dz * dz > 0.0004) {
        state.targetHeading = -Math.atan2(dx, dz)
      }
    }
    state.platformTarget.copy(replayVec)

    state.phase = phase
    if (progress < 1) state.stateName = 'running'
    state.replay.phase = phase
    state.replay.battery = state.battery
    state.replay.bins = {
      foam: state.binTarget[0],
      plastic: state.binTarget[1],
      mixed: state.binTarget[2],
    }
    state.progress = collect
    state.battery = Math.max(5, 100 - t * 28)
    state.binTarget = [
      collect * 0.72,
      collect * 0.58,
      collect * 0.5,
    ]
  }

  function startReplay({ route = [], speed = 1, home = null, target = null } = {}) {
    state.replay.active = route.length > 1
    state.replay.playing = state.replay.active
    state.replay.progress = 0
    state.replay.speed = Math.max(0.25, Number(speed) || 1)
    state.replay.route = route.map((point) => ({
      lng: Number(point.lng),
      lat: Number(point.lat),
    }))
    if (state.replay.active) {
      state.frozen = false
      fitTrackToRoute(state.replay.route, home, target)
      state.replay.targetIndex = nearestRouteIndex(state.replay.route, target)
      replayTelemetry(0)
    }
    return state.replay.active
  }

  function setReplaySpeed(speed) {
    state.replay.speed = Math.max(0.25, Number(speed) || 1)
  }

  function pauseReplay() {
    state.replay.playing = false
  }

  function resumeReplay() {
    if (!state.replay.active) return
    state.replay.playing = true
  }

  function restartReplay() {
    if (!state.replay.active) return
    state.replay.progress = 0
    state.replay.playing = true
    replayTelemetry(0)
  }

  function stopReplay() {
    state.replay.active = false
    state.replay.playing = false
    state.replay.progress = 0
    state.replay.route = []
  }

  function updateReplay(dt) {
    if (!state.replay.active) return
    if (state.replay.playing && state.replay.progress < 1) {
      state.replay.progress = Math.min(
        1,
        state.replay.progress + (dt * state.replay.speed) / REPLAY_SECONDS,
      )
    }
    if (state.replay.progress >= 1) {
      state.phase = 'done'
      state.stateName = 'done'
      state.progress = 1
      state.frozen = true
      state.poseTarget = PARK_POSE.slice()
      state.replay.playing = false
      return
    }
    state.frozen = false
    replayTelemetry(state.replay.progress)
  }

  // ------------------------------------------------------------- 姿态求解
  function resolvePose() {
    const phase = state.phase
    const stateName = state.stateName
    if (stateName === 'error' || stateName === 'stopped') return ERROR_POSE
    if (phase === 'done' || stateName === 'history') return PARK_POSE
    if (phase === 'collecting') return null // 由抓取循环生成
    if (phase === 'ack') return SCAN_POSE
    return STOW_POSE
  }

  let scoopFraction = 0

  function updateArm(dt) {
    const cycle = resolvePose()
    if (cycle) {
      state.poseTarget = cycle.slice()
      state.gripperTarget = 1
      arm.userData.carried.visible = false
      scoopFraction = 0
    } else {
      // 清理阶段：进度驱动固定次数的抓取循环，倍速自然加快
      const scoops = 5
      const raw = clamp(state.progress) * scoops
      const local = state.frozen ? scoopFraction : raw % 1
      scoopFraction = local
      state.poseTarget = sampleStops(SCOOP_STOPS, local)
      state.gripperTarget = local > 0.26 && local < 0.64 ? 0 : 1
      arm.userData.carried.visible = local > 0.32 && local < 0.8

      const hidden = Math.floor(clamp(state.progress) * DEBRIS_COUNT * 0.85)
      state.debrisHidden = hidden
    }
    if (cycle === STOW_POSE || cycle === SCAN_POSE) {
      const hidden = Math.floor(state.binTarget[0] * 20 + state.binTarget[1] * 20 + state.binTarget[2] * 20)
      state.debrisHidden = Math.min(DEBRIS_COUNT, hidden)
    }

    const idle = Math.sin(state.time * 0.8) * 0.012
    const damping = state.frozen ? 1 - Math.exp(-dt * 4) : 1 - Math.exp(-dt * 7)
    for (let i = 0; i < joints.length; i += 1) {
      const target = state.poseTarget[i] + idle
      state.pose[i] += (target - state.pose[i]) * damping
    }
    state.gripper += (state.gripperTarget - state.gripper) * (1 - Math.exp(-dt * 12))

    joints[0].rotation.y = state.pose[0]
    joints[1].rotation.x = state.pose[1]
    joints[2].rotation.x = state.pose[2]
    joints[3].rotation.y = state.pose[3]
    joints[4].rotation.x = state.pose[4]
    joints[5].rotation.y = state.pose[5]

    const spread = 0.08 + state.gripper * 0.06
    for (const handle of jointHandles) {
      handle.fingers[0].position.x = -spread
      handle.fingers[1].position.x = spread
    }
  }

  function updateRoute() {
    // 已走轨迹：home → 当前位置的一段带状折线
    const start = homeVec
    const end = state.platformPos
    const dir = tmpVec.copy(end).sub(start)
    const length = dir.length()
    const segments = Math.min(ROUTE_SEGMENTS, Math.max(1, Math.round(length * 2)))
    const visible = length > 0.2 ? segments : 0

    for (let i = 0; i < visible; i += 1) {
      const t0 = i / segments
      const t1 = (i + 1) / segments
      const p0 = new THREE.Vector3().lerpVectors(start, end, t0)
      const p1 = new THREE.Vector3().lerpVectors(start, end, t1)
      p0.y = 0.035 + waveHeight(p0.x, p0.z, state.time)
      p1.y = 0.035 + waveHeight(p1.x, p1.z, state.time)
      const n0 = sideVec.clone().multiplyScalar(0.075)
      const n1 = sideVec.clone().multiplyScalar(0.075)
      const base = i * 18
      ribbonPositions.set(
        [
          p0.x - n0.x, p0.y, p0.z - n0.z,
          p0.x + n0.x, p0.y, p0.z + n0.z,
          p1.x - n1.x, p1.y, p1.z - n1.z,
          p0.x + n0.x, p0.y, p0.z + n0.z,
          p1.x + n1.x, p1.y, p1.z + n1.z,
          p1.x - n1.x, p1.y, p1.z - n1.z,
        ],
        base,
      )
    }
    routeRibbonGeometry.attributes.position.needsUpdate = true
    routeRibbonGeometry.setDrawRange(0, visible * 6)

    // 剩余航线：当前位置 → 目标点的虚线圆点
    const remaining = tmpVec.copy(targetVec).sub(state.platformPos)
    const span = remaining.length()
    for (let i = 0; i < routeDots.count; i += 1) {
      const t = (i + 1) / (routeDots.count + 1)
      const point = new THREE.Vector3().lerpVectors(state.platformPos, targetVec, t)
      point.y = 0.04 + waveHeight(point.x, point.z, state.time)
      const visibleDot = span > 0.4
      tmpQuat.setFromEuler(new THREE.Euler(-Math.PI / 2, 0, 0))
      tmpScale.setScalar(visibleDot ? 1 - t * 0.25 : 0)
      tmpMatrix.compose(point, tmpQuat, tmpScale)
      routeDots.setMatrixAt(i, tmpMatrix)
    }
    routeDots.instanceMatrix.needsUpdate = true
  }

  function updateDebris() {
    const debrisAvailable = DEBRIS_COUNT - Math.min(DEBRIS_COUNT, state.debrisHidden)
    if (state.phase === 'collecting' && !state.debrisLocked) {
      state.debrisHeading = state.targetHeading
      state.debrisOrigin.copy(state.platformPos)
      state.debrisLocked = true
    }
    if (!['collecting', 'returning', 'done'].includes(state.phase)) {
      state.debrisLocked = false
    }
    const heading = state.debrisLocked ? state.debrisHeading : state.targetHeading
    const origin = state.debrisLocked ? state.debrisOrigin : state.platformPos
    const cosHeading = Math.cos(heading)
    const sinHeading = Math.sin(heading)
    for (let i = 0; i < DEBRIS_COUNT; i += 1) {
      const seed = debrisSeeds[i]
      const localX = seed.x
      const localZ = seed.z
      tmpVec2.set(
        origin.x + localX * cosHeading + localZ * sinHeading,
        0,
        origin.z - localX * sinHeading + localZ * cosHeading,
      )
      const bob = waveHeight(tmpVec2.x, tmpVec2.z, state.time)
      tmpVec2.y = 0.06 + bob

      const alive = i < debrisAvailable
      const scale = alive ? seed.scale : 0
      tmpQuat.setFromEuler(
        new THREE.Euler(
          Math.sin(state.time * 0.7 + seed.spin) * 0.16,
          seed.spin + state.time * 0.12,
          Math.cos(state.time * 0.6 + seed.spin) * 0.1 + seed.tilt,
        ),
      )
      tmpScale.setScalar(scale)
      tmpMatrix.compose(tmpVec2, tmpQuat, tmpScale)
      debris.setMatrixAt(i, tmpMatrix)
    }
    debris.instanceMatrix.needsUpdate = true
  }

  function updatePlatform(dt) {
    if (!state.frozen) {
      state.missionTime += dt * Math.max(1, state.speed) * 0.35
    }

    const damping = 1 - Math.exp(-dt * 2.4)
    state.platformPos.lerp(state.platformTarget, damping)

    const bob = waveHeight(state.platformPos.x, state.platformPos.z, state.time)
    const moving = ['navigating', 'returning'].includes(state.phase)
    const rollAmp = moving ? 0.05 : 0.022
    const pitchAmp = moving ? 0.035 : 0.016

    platform.position.set(
      state.platformPos.x,
      0.05 + bob,
      state.platformPos.z,
    )
    platform.rotation.z = Math.sin(state.time * 1.25) * rollAmp
    platform.rotation.x = Math.sin(state.time * 0.95 + 1.4) * pitchAmp

    // 航向对齐（取最短弧）
    let delta = state.targetHeading - platform.rotation.y
    while (delta > Math.PI) delta -= Math.PI * 2
    while (delta < -Math.PI) delta += Math.PI * 2
    platform.rotation.y += delta * (1 - Math.exp(-dt * 3))

    platform.userData.wake.opacity = moving ? 0.34 : 0.08
    if (platform.userData.wake.opacity > 0) {
      const pulse = 1 + Math.sin(state.time * 4) * 0.05
      platform.userData.wake.map.rotation = state.time * 0.2
      const wakeMesh = platform.children.find((child) => child.material === platform.userData.wake)
      if (wakeMesh) wakeMesh.scale.set(pulse, 1 + (moving ? 0.35 : 0), 1)
    }

    if (platform.userData.radar) {
      platform.userData.radar.rotation.y = state.time * (moving ? 1.9 : 0.9)
    }

    const blink = 0.5 + 0.5 * Math.sin(state.time * 4.2)
    const beaconColor =
      state.stateName === 'paused'
        ? PALETTE.warning
        : ['stopped', 'error'].includes(state.stateName)
          ? 0xff3b30
          : PALETTE.accentBright
    platform.userData.beacon.material = matAccent
    platform.userData.beaconGlow.material.color.setHex(beaconColor)
    platform.userData.beaconGlow.material.opacity = 0.35 + blink * 0.5

    // 料仓液位
    for (let i = 0; i < 3; i += 1) {
      state.binCurrent[i] += (state.binTarget[i] - state.binCurrent[i]) * (1 - Math.exp(-dt * 4))
      const fill = platform.userData.binFills[i]
      if (fill) fill.scale.y = Math.max(0.001, clamp(state.binCurrent[i]))
    }

    // 跟随视角
    if (state.follow) {
      const desired = tmpVec.copy(state.platformPos)
      desired.y = 0.95
      const shift = desired.sub(controls.target).multiplyScalar(1 - Math.exp(-dt * 2.6))
      controls.target.add(shift)
      camera.position.add(shift)
    }

    // 阴影相机跟随平台，保证近景阴影分辨率
    sun.position.set(state.platformPos.x + 7, 10, state.platformPos.z + 6)
    sun.target.position.copy(state.platformPos)
    sun.target.updateMatrixWorld()
  }

  function updateTargetRing() {
    const pulse = 0.5 + 0.5 * Math.sin(state.time * 1.6)
    const active = ['navigating', 'collecting'].includes(state.phase)
    targetRing.scale.setScalar(1 + pulse * 0.09)
    targetRingMaterial.opacity = active ? 0.35 + pulse * 0.35 : 0.16 + pulse * 0.12
  }

  let jointTimer = 0
  let lastFrame = performance.now()
  let running = true

  function frame() {
    if (!running) return
    const now = performance.now()
    const dt = Math.min(0.05, Math.max(0.001, (now - lastFrame) / 1000))
    lastFrame = now
    state.time += dt

    seaUniforms.uTime.value = state.time
    updateReplay(dt)
    updatePlatform(dt)
    updateArm(dt)
    updateRoute()
    updateDebris()
    updateTargetRing()

    jointTimer += dt
    if (jointTimer > 0.12) {
      jointTimer = 0
      onJoints?.(state.pose.map((value) => (value * 180) / Math.PI))
    }

    controls.update()
    renderer.render(scene, camera)
    requestAnimationFrame(frame)
  }

  function resize() {
    const parent = canvas.parentElement
    if (!parent) return
    const width = Math.max(1, parent.clientWidth)
    const height = Math.max(1, parent.clientHeight)
    camera.aspect = width / height
    camera.updateProjectionMatrix()
    renderer.setSize(width, height, false)
  }

  function resetView() {
    const target = tmpVec.copy(state.platformPos)
    camera.position
      .copy(target)
      .addScaledVector(sideVec, 7.2)
      .addScaledVector(axis, -3.4)
      .add(new THREE.Vector3(0, 5.1, 0))
    controls.target.set(target.x, target.y + 1.0, target.z)
    controls.update()
  }

  function setFollow(value) {
    state.follow = Boolean(value)
  }

  function setTheme(isDark) {
    const horizon = new THREE.Color(isDark ? PALETTE.horizonDark : PALETTE.horizonLight)
    seaUniforms.uHorizon.value.copy(horizon)
    skyUniforms.uHorizon.value.copy(horizon)
    skyUniforms.uTop.value.setHex(isDark ? PALETTE.skyTopDark : PALETTE.skyTopLight)
    skyUniforms.uBottom.value.setHex(isDark ? PALETTE.skyBottomDark : PALETTE.skyBottomLight)
    sun.intensity = isDark ? 1.9 : 2.4
    hemi.intensity = isDark ? 0.85 : 1.15
  }

  function dispose() {
    running = false
    controls.dispose()
    scene.traverse((child) => {
      if (child.isMesh || child.isInstancedMesh) {
        child.geometry?.dispose?.()
      }
    })
    for (const item of disposables) item?.dispose?.()
    renderer.dispose()
  }

  controls.addEventListener('start', () => {
    state.follow = false
  })

  applyTelemetry({ home: { lng: 119.86, lat: 26.35 }, target: { lng: 119.86, lat: 26.36 } })
  applyTrack()
  setTheme(document.documentElement.getAttribute('data-theme') === 'dark')
  resize()
  resetView()
  requestAnimationFrame(frame)

  return {
    applyTelemetry,
    startReplay,
    setReplaySpeed,
    pauseReplay,
    resumeReplay,
    restartReplay,
    stopReplay,
    resize,
    resetView,
    setFollow,
    setTheme,
    dispose,
    get follow() {
      return state.follow
    },
    get replay() {
      return state.replay
    },
    labelFor(index) {
      return JOINT_LABELS[index] || `J${index + 1}`
    },
  }
}
