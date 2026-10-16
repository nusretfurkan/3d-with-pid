// Display-only studio for the engineering scene (world metres, right-handed, Z up).
// Nothing here is engineering geometry: the floor, grid, lights, environment and
// contact shadow exist only to make the CAD tessellation readable. Original code;
// the general techniques (softbox environment, seamless floor fade, blurred contact
// shadow) are common real-time rendering practice.

import * as THREE from 'three';

export const PAPER = 0xf1f0eb; // canvas backdrop, matches --paper-3d in app.css
const DISPLAY_GRID = [0.25, 1.0, 0.05, 0.11]; // minor m, major m, minor strength, major strength
const FLOOR_GAP = 0.0015; // floor sits just below the lowest geometry

function softboxEnvironment(renderer) {
  // A neutral studio: one large overhead softbox, a warm key strip, two cool rims, a low
  // fill and dark flags so metallic parts keep readable reflections. Z is up.
  const env = new THREE.Scene();
  const disposables = [];
  const add = (geometry, material, setup) => {
    const mesh = new THREE.Mesh(geometry, material);
    setup(mesh);
    env.add(mesh);
    disposables.push(geometry, material);
  };
  add(new THREE.SphereGeometry(40, 32, 16), new THREE.MeshBasicMaterial({ color: 0xb4b2ab, side: THREE.BackSide }), () => {});
  add(new THREE.CircleGeometry(40, 48), new THREE.MeshBasicMaterial({ color: 0xdedbd2 }), (m) => { m.position.z = -2.5; });
  const panel = (w, h, position, intensity, tint = 0xffffff) => add(
    new THREE.PlaneGeometry(w, h),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(tint).multiplyScalar(intensity), side: THREE.DoubleSide }),
    (m) => { m.position.set(...position); m.lookAt(0, 0, 0); },
  );
  panel(16, 10, [0, -2, 15], 2.0);              // overhead softbox
  panel(5, 12, [-13, -10, 6], 4.0, 0xfff1e2);   // warm key strip, front left
  panel(2.6, 12, [12, 9, 6], 5.2, 0xe4edff);    // cool rim, back right
  panel(2.6, 12, [-12, 11, 5], 2.8);            // rim, back left
  panel(10, 3, [5, -14, 2], 1.2);               // low front fill
  panel(6, 16, [14, -6, 6], 0.03);              // dark flags for metal contrast
  panel(5, 16, [-15, 1, 6], 0.03);
  panel(18, 3, [0, 15, 1], 0.04);
  const pmrem = new THREE.PMREMGenerator(renderer);
  const target = pmrem.fromScene(env, 0.035);
  pmrem.dispose();
  for (const d of disposables) d.dispose();
  return target.texture;
}

function floorMaterial(background) {
  const material = new THREE.MeshStandardMaterial({ color: 0xeceae3, roughness: 0.96, metalness: 0, envMapIntensity: 0.35 });
  // The fade is applied after tone mapping and output conversion, so it needs the raw sRGB
  // components of the backdrop hex (setHex with a linear space skips the sRGB decode).
  const raw = new THREE.Color().setHex(background, THREE.LinearSRGBColorSpace);
  const uniforms = {
    uBg: { value: new THREE.Vector3(raw.r, raw.g, raw.b) },
    uCenter: { value: new THREE.Vector2(0, 0) },
    uFade: { value: new THREE.Vector2(3, 12) },
    uGrid: { value: new THREE.Vector4(...DISPLAY_GRID) },
  };
  material.userData.uniforms = uniforms;
  material.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nvarying vec3 vFloorWorld;')
      .replace('#include <project_vertex>', '#include <project_vertex>\nvFloorWorld = (modelMatrix * vec4(transformed, 1.0)).xyz;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', `#include <common>
varying vec3 vFloorWorld;
uniform vec3 uBg;
uniform vec2 uCenter;
uniform vec2 uFade;
uniform vec4 uGrid;
float floorGrid(vec2 p, float spacing) {
  vec2 q = p / spacing;
  vec2 w = fwidth(q);
  vec2 g = abs(fract(q - 0.5) - 0.5) / max(w, vec2(1e-4));
  return 1.0 - min(min(g.x, g.y), 1.0);
}`)
      .replace('#include <color_fragment>', `#include <color_fragment>
float floorDist = length(vFloorWorld.xy - uCenter);
float gridFade = 1.0 - smoothstep(uFade.x * 0.6, uFade.y * 0.7, floorDist);
float lines = max(floorGrid(vFloorWorld.xy, uGrid.x) * uGrid.z, floorGrid(vFloorWorld.xy, uGrid.y) * uGrid.w);
diffuseColor.rgb *= 1.0 - lines * gridFade;`)
      .replace('#include <dithering_fragment>', `#include <dithering_fragment>
float horizon = smoothstep(uFade.x, uFade.y, length(vFloorWorld.xy - uCenter));
gl_FragColor.rgb = mix(gl_FragColor.rgb, uBg, horizon);`);
  };
  return material;
}

const BLUR_VERTEX = `varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }`;
const BLUR_FRAGMENT = `uniform sampler2D tMap; uniform vec2 uStep; varying vec2 vUv;
void main() {
  vec4 sum = texture2D(tMap, vUv) * 0.2270270270;
  sum += texture2D(tMap, vUv + uStep * 1.3846153846) * 0.3162162162;
  sum += texture2D(tMap, vUv - uStep * 1.3846153846) * 0.3162162162;
  sum += texture2D(tMap, vUv + uStep * 3.2307692308) * 0.0702702703;
  sum += texture2D(tMap, vUv - uStep * 3.2307692308) * 0.0702702703;
  gl_FragColor = sum;
}`;

/** Soft darkening under parts that are close to the floor (rendered from below, blurred). */
class GroundContact {
  constructor(scene) {
    this.size = 512;
    const opts = { minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter, type: THREE.HalfFloatType };
    this.rtA = new THREE.WebGLRenderTarget(this.size, this.size, opts);
    this.rtB = new THREE.WebGLRenderTarget(this.size, this.size, opts);
    this.camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    this.camera.up.set(0, 1, 0);
    this.reach = 0.35;
    this.heightMaterial = new THREE.ShaderMaterial({
      uniforms: { uFloor: { value: 0 }, uReach: { value: this.reach } },
      vertexShader: `uniform float uFloor; varying float vHeight;
        void main() { vec4 w = modelMatrix * vec4(position, 1.0); vHeight = w.z - uFloor; gl_Position = projectionMatrix * viewMatrix * w; }`,
      fragmentShader: `uniform float uReach; varying float vHeight;
        void main() { float k = 1.0 - clamp(vHeight / uReach, 0.0, 1.0); gl_FragColor = vec4(0.0, 0.0, 0.0, k * k * 0.9); }`,
      transparent: true, depthTest: false, depthWrite: false, side: THREE.DoubleSide,
    });
    this.blurMaterial = new THREE.ShaderMaterial({
      uniforms: { tMap: { value: null }, uStep: { value: new THREE.Vector2() } },
      vertexShader: BLUR_VERTEX, fragmentShader: BLUR_FRAGMENT, depthTest: false, depthWrite: false,
    });
    this.quadScene = new THREE.Scene();
    this.quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.blurMaterial);
    this.quad.frustumCulled = false;
    this.quadScene.add(this.quad);
    this.quadCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    this.plane = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.ShaderMaterial({
      uniforms: { tMap: { value: this.rtA.texture }, uColor: { value: new THREE.Color(0x2a2620) }, uOpacity: { value: 0.72 } },
      vertexShader: 'varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
      // The camera looks up (+Z) with +Y up, so world +X is to the left of its image: mirror u.
      fragmentShader: `uniform sampler2D tMap; uniform vec3 uColor; uniform float uOpacity; varying vec2 vUv;
        void main() { float a = texture2D(tMap, vec2(1.0 - vUv.x, vUv.y)).a; gl_FragColor = vec4(uColor, a * uOpacity); }`,
      transparent: true, depthWrite: false, toneMapped: false,
    }));
    this.plane.renderOrder = 2;
    this.plane.userData.studio = true;
    this.plane.visible = false;
    scene.add(this.plane);
  }

  setBounds(min, max, floorZ) {
    const cx = (min[0] + max[0]) / 2;
    const cy = (min[1] + max[1]) / 2;
    const half = Math.max(max[0] - min[0], max[1] - min[1]) / 2 + 0.6;
    this.camera.left = -half; this.camera.right = half; this.camera.top = half; this.camera.bottom = -half;
    this.camera.near = 0; this.camera.far = this.reach;
    this.camera.position.set(cx, cy, floorZ);
    this.camera.lookAt(cx, cy, floorZ + 1);
    this.camera.updateProjectionMatrix();
    this.camera.updateMatrixWorld();
    this.heightMaterial.uniforms.uFloor.value = floorZ;
    this.plane.position.set(cx, cy, floorZ + 0.0006);
    this.plane.scale.set(half * 2, half * 2, 1);
    this.plane.visible = true;
  }

  dispose() {
    this.rtA.dispose();
    this.rtB.dispose();
    this.heightMaterial.dispose();
    this.blurMaterial.dispose();
    this.quad.geometry.dispose();
    this.plane.geometry.dispose();
    this.plane.material.dispose();
  }

  update(renderer, scene, hidden) {
    const previous = {
      target: renderer.getRenderTarget(), override: scene.overrideMaterial, background: scene.background,
      clear: renderer.getClearColor(new THREE.Color()), alpha: renderer.getClearAlpha(),
    };
    const restore = [];
    for (const obj of hidden) if (obj.visible) { obj.visible = false; restore.push(obj); }
    scene.overrideMaterial = this.heightMaterial;
    scene.background = null;
    renderer.setClearColor(0x000000, 0);
    renderer.setRenderTarget(this.rtA);
    renderer.clear();
    renderer.render(scene, this.camera);
    scene.overrideMaterial = previous.override;
    scene.background = previous.background;
    for (const obj of restore) obj.visible = true;
    const texel = 1 / this.size;
    for (const spread of [2.2, 1.0]) {
      this.blurMaterial.uniforms.tMap.value = this.rtA.texture;
      this.blurMaterial.uniforms.uStep.value.set(texel * spread, 0);
      renderer.setRenderTarget(this.rtB);
      renderer.render(this.quadScene, this.quadCamera);
      this.blurMaterial.uniforms.tMap.value = this.rtB.texture;
      this.blurMaterial.uniforms.uStep.value.set(0, texel * spread);
      renderer.setRenderTarget(this.rtA);
      renderer.render(this.quadScene, this.quadCamera);
    }
    renderer.setRenderTarget(previous.target);
    renderer.setClearColor(previous.clear, previous.alpha);
  }
}

export class Studio {
  constructor(renderer, scene) {
    this.renderer = renderer;
    this.scene = scene;
    scene.background = new THREE.Color(PAPER);
    scene.environment = softboxEnvironment(renderer);
    scene.environmentIntensity = 0.85;

    this.floor = new THREE.Mesh(new THREE.PlaneGeometry(60, 60), floorMaterial(PAPER));
    this.floor.receiveShadow = true;
    this.floor.userData.studio = true;
    scene.add(this.floor);

    this.key = new THREE.DirectionalLight(0xfff4e8, 2.4);
    this.key.castShadow = true;
    this.key.shadow.mapSize.set(2048, 2048);
    this.key.shadow.bias = -0.00015;
    this.key.shadow.normalBias = 0.004;
    scene.add(this.key, this.key.target);
    this.rim = new THREE.DirectionalLight(0xdfe8ff, 0.7);
    scene.add(this.rim, this.rim.target);
    this.fill = new THREE.HemisphereLight(0xffffff, 0xd8d4ca, 0.35);
    this.fill.position.set(0, 0, 1);
    scene.add(this.fill);

    this.contact = new GroundContact(scene);
    this.contactDirty = true;
    this.floorZ = 0;
    this.bounds = null;
    this.referencePlane = false;
  }

  /**
   * Coordinates mode: the viewer draws the world XY reference grid at Z = 0 itself, so the floor's
   * display grid is hidden (never two conflicting grids) and the floor is kept at or below Z = 0 so
   * that reference plane stays visible. Off restores the display studio exactly.
   */
  setReferencePlane(on) {
    this.referencePlane = !!on;
    const grid = this.floor.material.userData.uniforms.uGrid.value;
    grid.z = this.referencePlane ? 0 : DISPLAY_GRID[2];
    grid.w = this.referencePlane ? 0 : DISPLAY_GRID[3];
    if (this.bounds) this.setBounds(this.bounds.min, this.bounds.max);
    this.invalidate();
  }

  setBounds(min, max) {
    this.bounds = { min: min.slice(), max: max.slice() };
    const center = new THREE.Vector3((min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2);
    const radius = Math.max(0.5, Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2);
    this.floorZ = (this.referencePlane ? Math.min(min[2], 0) : min[2]) - FLOOR_GAP;
    this.floor.position.set(center.x, center.y, this.floorZ);
    const u = this.floor.material.userData.uniforms;
    u.uCenter.value.set(center.x, center.y);
    u.uFade.value.set(radius * 2.2, radius * 7);
    const keyDir = new THREE.Vector3(-0.55, -0.95, 1.55).normalize();
    this.key.position.copy(center).addScaledVector(keyDir, radius * 4);
    this.key.target.position.copy(center);
    const cam = this.key.shadow.camera;
    cam.left = -radius * 1.25; cam.right = radius * 1.25; cam.top = radius * 1.25; cam.bottom = -radius * 1.25;
    cam.near = radius * 0.5; cam.far = radius * 8;
    cam.updateProjectionMatrix();
    this.rim.position.copy(center).addScaledVector(new THREE.Vector3(0.9, 1.2, 0.8).normalize(), radius * 4);
    this.rim.target.position.copy(center);
    this.contact.setBounds(min, max, this.floorZ);
    this.invalidate();
  }

  /** Free GPU resources owned by the studio (the renderer itself is disposed by its viewer). */
  dispose() {
    if (this.scene.environment) this.scene.environment.dispose();
    this.floor.geometry.dispose();
    this.floor.material.dispose();
    this.key.shadow.dispose();
    this.contact.dispose();
  }

  invalidate() {
    this.contactDirty = true;
    this.renderer.shadowMap.needsUpdate = true;
  }

  beforeRender(hiddenDuringContact) {
    if (this.contactDirty) {
      this.contact.update(this.renderer, this.scene, [this.floor, this.contact.plane, ...hiddenDuringContact]);
      this.contactDirty = false;
    }
  }
}
