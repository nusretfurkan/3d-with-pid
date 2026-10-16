// Interactive 3D view of the generator's scene.json (schema 1): WORLD metres,
// right-handed, Z up. Meshes are exactly the CAD tessellation supplied by the
// geometry generator; this module never builds or rescales equipment geometry.
// Floor, grid, lights, edges, labels and selection markers are display-only.
//
// Coordinates (per view, off by default) draw the world origin, labelled X/Y/Z axes and
// an XY reference grid at Z = 0, and read out the selected item's authoritative placement
// origin from the current model with its source. Move (off by default) attaches a world-space
// translation gizmo to that origin: the view previews one world delta on every owner mesh,
// edge, label and port of the installed scene and reports intent through callbacks; the
// application owns the move record and the validated command. Nothing here writes project data.
//
// A pick on a line's pipe sweep also names the straight section under the hit point (computed from
// the current authoritative route, only on a final scene of exactly the current revision and digest).
// The selected section is drawn as a display-only sleeve, and with Move on a second gizmo offers only
// the world axes perpendicular to it; a drag ends in a reviewable, tentative route preview drawn as an
// overlay (the installed pipe mesh is only faded). Apply happens in the pane's section panel.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { Studio } from './studio.js';
import { layoutLabels } from './labels-layout.js';
import { appearanceKey, isFinalScene, planSceneUpdate } from './scene-diff.js';
import {
  DEFAULT_SNAP_M, MOVE_REASONS, applyDelta, dragEndReason, effectivePreview, isFiniteVec3, isNoOp, moveEligibility, normalizeSnap,
  sameBase, selectionOwner, snapPosition, sourceGate,
} from './equipment-move.js';
import { MAX_OFFSET_M, offsetVector, pipeRadius, routeKey, routeOf, sectionAtPoint, straightSections } from './pipe-sections.js';
import { PIPE_START_REASONS } from './pipe-move-coordinator.js';
import { coordinateReadout, formatMetres, gridExtent, gridLines } from './coordinates.js';

THREE.Object3D.DEFAULT_UP.set(0, 0, 1);

const AMBER = new THREE.Color(0xe8a317);
const GHOST_TINT = new THREE.Color(0x9aa3ab);
const VIEW_DIRECTIONS = {
  iso: [0.64, -1, 0.72],
  front: [0, -1, 0],
  right: [1, 0, 0],
  top: [0, -1e-4, 1],
  rear: [-0.64, 1, 0.72],
};
const SVG_NS = 'http://www.w3.org/2000/svg';
const AXIS_COLORS = { X: 0xb3261e, Y: 0x2f7d3a, Z: 0x1f4f8f };
// TransformControls' handle colours, restyled once to the axis colours above (no file changes).
const GIZMO_TINT = { 0xff0000: AXIS_COLORS.X, 0x00ff00: AXIS_COLORS.Y, 0x0000ff: AXIS_COLORS.Z };
const EXCLUDED_LAYER = 31; // neither rendered nor picked: the gizmo's free screen-plane handle
// Candidate points of the translate handles in gizmo units (positive arrow shafts, plane squares);
// the first is the nominal point, the others are tried when another handle covers it on screen.
const HANDLE_POINTS = (() => {
  const axis = [0.45, 0.35, 0.55, 0.28];
  const plane = [[0.15, 0.15], [0.22, 0.22], [0.1, 0.22], [0.22, 0.1], [0.08, 0.08]];
  const put = (i, j, [a, b]) => { const p = [0, 0, 0]; p[i] = a; p[j] = b; return p; };
  return [
    ['X', axis.map((a) => [a, 0, 0])], ['Y', axis.map((a) => [0, a, 0])], ['Z', axis.map((a) => [0, 0, a])],
    ['XY', plane.map((q) => put(0, 1, q))], ['YZ', plane.map((q) => put(1, 2, q))], ['XZ', plane.map((q) => put(0, 2, q))],
  ];
})();
const AXIS_UNIT = { X: [1, 0, 0], Y: [0, 1, 0], Z: [0, 0, 1] };
const HANDLE_LABEL_AT = 0.72;
const WORLD_NOTE = 'World XYZ · metres · Z up';

function clamp01(v, fallback) {
  return typeof v === 'number' && Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : fallback;
}

/** Buffers of an already validated component (see scene-diff.geometryProblem). */
function makeGeometry(c) {
  const positions = c.positions;
  const indices = c.indices;
  const count = positions.length / 3;
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  if (Array.isArray(indices) && indices.length) {
    geometry.setIndex(new THREE.BufferAttribute(count <= 65536 ? new Uint16Array(indices) : new Uint32Array(indices), 1));
  }
  if (Array.isArray(c.normals) && c.normals.length === positions.length) {
    geometry.setAttribute('normal', new THREE.Float32BufferAttribute(c.normals, 3));
  } else {
    geometry.computeVertexNormals();
  }
  geometry.computeBoundingBox();
  geometry.computeBoundingSphere();
  return geometry;
}

function div(className, text) {
  const el = document.createElement('div');
  el.className = className;
  if (text != null) el.textContent = text;
  return el;
}

export class Viewer3D {
  constructor(host, callbacks = {}) {
    this.host = host;
    this.cb = callbacks;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.NeutralToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.shadowMap.autoUpdate = false;
    this.canvas = this.renderer.domElement;
    this.canvas.className = 'gl';
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute('aria-label', '3D view of the generated equipment scene');
    host.append(this.canvas);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(32, 1, 0.01, 200);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(4, -5, 3);
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.14;
    this.controls.screenSpacePanning = true;
    this.controls.zoomToCursor = true;
    this.controls.minDistance = 0.04;
    this.controls.maxDistance = 80;
    this.controls.maxPolarAngle = Math.PI / 2 - 0.015; // stay above the display floor
    this.controls.addEventListener('change', () => { this.dirty = true; });
    this.controls.addEventListener('start', () => { this.tween = null; });

    this.studio = new Studio(this.renderer, this.scene);
    this.root = new THREE.Group();
    this.root.name = 'engineering-scene';
    this.edgeGroup = new THREE.Group();
    this.edgeGroup.name = 'display-edges';
    this.markers = new THREE.Group();
    this.markers.name = 'selection-markers';
    // display-only interaction helpers: world grid/axes and the base-position ghost of a move preview
    this.helpers = new THREE.Group();
    this.helpers.name = 'interaction-helpers';
    this.ghostGroup = new THREE.Group();
    this.ghostGroup.name = 'move-preview-base';
    this.helpers.add(this.ghostGroup);
    this.moveProxy = new THREE.Object3D(); // the gizmo's target: the equipment origin, never a component
    this.moveProxy.name = 'move-origin';
    this.sectionGroup = new THREE.Group(); // selected straight-section sleeve and tentative route preview
    this.sectionGroup.name = 'pipe-section-overlay';
    this.helpers.add(this.sectionGroup);
    this.pipeProxy = new THREE.Object3D(); // the section gizmo's target: the section midpoint
    this.pipeProxy.name = 'pipe-section-origin';
    this.scene.add(this.root, this.edgeGroup, this.markers, this.helpers, this.moveProxy, this.pipeProxy);

    this.components = new Map();
    this.materials = new Map();
    this.variants = new Map();
    this.edgeMaterials = {
      base: new THREE.LineBasicMaterial({ color: 0x1b2026, transparent: true, opacity: 0.22, depthWrite: false }),
      selected: new THREE.LineBasicMaterial({ color: 0xc98a00, transparent: true, opacity: 0.95, depthWrite: false }),
      ghost: new THREE.LineBasicMaterial({ color: 0x6b7178, transparent: true, opacity: 0.08, depthWrite: false }),
    };
    this.labels = new Map();
    this.ports = new Map();
    this.selected = new Set();
    this.selectedIds = null; // semantic IDs of the selection when the caller supplies them
    this.hovered = null;
    this.edgesOn = true;
    this.labelMode = 'all';
    this.bounds = null;
    this.hasScene = false;
    this.framed = false; // the camera has been fitted to visible components once (an empty project has none yet)
    this.dirty = true;
    this.tween = null;
    this.flashing = new Set();
    this.raycaster = new THREE.Raycaster();
    this.pickables = [];

    this.coordinatesOn = false;
    this.coord = null;
    this.context = null;       // {project, inputDigest, online, mode, installedSource, move} from the application
    this.moveEnabled = false;
    this.moveSnap = DEFAULT_SNAP_M;
    this.moveLocal = null;     // this view's own gizmo gesture
    this.moveGate = null;      // last eligibility result
    this.preview = null;       // preview drawn now (equipment-move.effectivePreview)
    this.portPreview = new Map();
    this.transform = null;     // TransformControls, created on the first setMoveEnabled(true)
    this.ghostMaterial = null;
    this.readout = null;
    this.pipeTransform = null; // TransformControls of a straight pipe section, created on demand
    this.pipeLocal = null;     // this view's own section drag
    this.pipeGate = null;
    this.sectionDrawn = null;  // {highlight, preview} drawn now

    this._buildOverlay();
    this._bindPointer();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.canvas.addEventListener('webglcontextlost', (event) => {
      event.preventDefault();
      if (this.disposed) return; // released on purpose by dispose()
      if (this.cb.onError) this.cb.onError('The WebGL context was lost; reload the page to restore the 3D view.');
    });
    this.active = true;
    this.disposed = false;
    this.keepCamera = false;
    const initial = callbacks.initialView;
    if (initial) {
      if (initial.camera) {
        this.setCameraState(initial.camera);
        this.keepCamera = true; // the pane's own camera survives a type switch: no automatic iso fit
      }
      if (initial.labelMode) this.setLabelMode(initial.labelMode);
      if (typeof initial.edgesOn === 'boolean') this.edgesOn = initial.edgesOn;
      if (initial.coordinatesOn === true) this.setCoordinates(true);
    }
    this.resize();
    this._loop = this._loop.bind(this);
    this.rafId = requestAnimationFrame(this._loop);
  }

  /** Pause (hidden pane) or resume rendering; scene data and camera are kept. */
  setActive(on) {
    this.active = !!on;
    if (!this.active) {
      this._cancelLocal('hidden');
      this._pipeAbort('hidden');
    }
    this._syncGizmo();
    this._syncPipe();
    if (this.active && !this.disposed && this.rafId == null) {
      this.resize();
      this.dirty = true;
      this.rafId = requestAnimationFrame(this._loop);
    }
  }

  /** Navigation and display options of this view, for restoring a later view of the same pane. */
  viewState() {
    return { camera: this.cameraState(), labelMode: this.labelMode, edgesOn: this.edgesOn, coordinatesOn: this.coordinatesOn };
  }

  setCameraState(state) {
    if (!state || !Array.isArray(state.position) || !Array.isArray(state.target)) return;
    this.camera.position.fromArray(state.position);
    this.controls.target.fromArray(state.target);
    const distance = this.camera.position.distanceTo(this.controls.target);
    this.camera.near = Math.max(0.002, distance / 400);
    this.camera.far = distance * 40 + 40;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.tween = null;
    this.dirty = true;
  }

  /** Release the WebGL context, GPU resources, observers and DOM of this view. */
  dispose() {
    if (this.disposed) return;
    this._disposing = true;
    try {
      this._cancelLocal('destroyed'); // an unsubmitted preview ends with the view
    } catch (error) {
      console.error(error);
    }
    this.disposed = true;
    this.active = false;
    this.moveLocal = null;
    this._escapeOff();
    if (this._orbitHoldOff) this._orbitHoldOff();
    if (this.rafId != null) cancelAnimationFrame(this.rafId);
    this.rafId = null;
    this.resizeObserver.disconnect();
    if (this.transform) {
      this.transform.detach();
      this.transform.dispose(); // its listeners and every handle geometry/material
      this.scene.remove(this.transform.getHelper());
      this.transform = null;
    }
    this.pipeLocal = null;
    if (this._pipeEscapeBound) window.removeEventListener('keydown', this._onPipeEscape, true);
    if (this.pipeTransform) {
      this.pipeTransform.detach();
      this.pipeTransform.dispose();
      this.scene.remove(this.pipeTransform.getHelper());
      this.pipeTransform = null;
    }
    this._clearSection();
    this.controls.dispose();
    this.clear();
    this._disposeCoordinates();
    if (this.ghostMaterial) this.ghostMaterial.dispose();
    for (const child of [...this.markers.children]) {
      child.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
    }
    for (const m of this.materials.values()) m.dispose();
    for (const m of this.variants.values()) m.dispose();
    for (const m of Object.values(this.edgeMaterials)) m.dispose();
    this.materials.clear();
    this.variants.clear();
    this.studio.dispose();
    this.renderer.renderLists.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.canvas.remove();
    this.overlay.remove();
  }

  // ---------------------------------------------------------------- scene data

  /**
   * Apply one scene snapshot incrementally. Components are matched by stable ID; geometry is
   * reused only when its buffers are exactly equal, restyled when only the appearance changed,
   * and rebuilt otherwise (scene-diff.planSceneUpdate). In a working preview, components the
   * generator has not reached yet stay visible as ghosts of the previous view; a final scene
   * removes them.
   */
  setScene(json, source) {
    if (!json || json.schema_version !== 1 || json.units !== 'm' || json.up_axis !== 'Z' || !Array.isArray(json.components)) {
      throw new Error('scene.json is not a schema-1 WORLD-metre Z-up scene');
    }
    const final = isFinalScene(json);
    const first = !this.hasScene;
    const now = performance.now();
    const plan = planSceneUpdate(this.components, json.components, final);
    const stats = {
      added: plan.add.length, changed: plan.replace.length, restyled: plan.restyle.length, reused: plan.keep.length,
      removed: plan.remove.length, ghosted: plan.ghost.length, invalid: plan.invalid, triangles: 0,
    };
    for (const c of plan.keep) {
      const rec = this.components.get(c.id);
      rec.ghost = false;
      Object.assign(rec, this._meta(c));
    }
    for (const c of plan.restyle) {
      const rec = this.components.get(c.id);
      rec.ghost = false;
      Object.assign(rec, this._meta(c));
      rec.base = this._baseMaterial(c);
      rec.appearance = appearanceKey(c);
      rec.mesh.castShadow = !c.display_only;
      this._flash(rec, first, now);
    }
    for (const c of [...plan.replace, ...plan.add]) {
      const previous = this.components.get(c.id);
      if (previous) this._disposeRecord(previous);
      const rec = this._createRecord(c, makeGeometry(c));
      this.components.set(c.id, rec);
      this._flash(rec, first, now);
    }
    for (const id of plan.remove) {
      this._disposeRecord(this.components.get(id));
      this.components.delete(id);
    }
    for (const id of plan.ghost) this.components.get(id).ghost = true;
    for (const rec of this.components.values()) if (!rec.ghost) stats.triangles += rec.triangles;
    this._applyLabels(json.labels, final);
    this.ports = new Map((Array.isArray(json.ports) ? json.ports : []).filter((p) => p && p.id).map((p) => [p.id, p]));
    this._updateBounds(json.bounds_m, final);
    this.pickables = Array.from(this.components.values(), (r) => r.mesh);
    this.source = { ...source, final, status: json.status, revision: json.source_revision, triangles: json.triangle_count };
    this.hasScene = true;
    // Frame once, on the first scene that has visible components: a blank project's empty first scene must
    // not use up the initial framing, or the first equipment added later would stay outside the view.
    const content = [...this.components.values()].some((rec) => !rec.ghost);
    if ((first || !this.framed) && content && !this.keepCamera) this.setView('iso', false);
    if (content || this.keepCamera) this.framed = true;
    this._refreshLooks();
    this.studio.invalidate();
    this.dirty = true;
    this._sceneChangedForMove();
    return stats;
  }

  _meta(c) {
    return {
      semanticId: c.semantic_id, key: c.component_key, tag: c.tag, kind: c.kind, status: c.status,
      displayOnly: !!c.display_only, assumptions: Array.isArray(c.assumptions) ? c.assumptions : [],
    };
  }

  _flash(rec, first, now) {
    if (first) return;
    if (rec.flashMaterial) rec.flashMaterial.dispose(); // re-cloned from the current base on the next frame
    rec.flashMaterial = null;
    rec.flashStart = now; // a real change arrived: brief amber highlight, no delay to the data
    this.flashing.add(rec);
  }

  _createRecord(c, geometry) {
    const base = this._baseMaterial(c);
    const mesh = new THREE.Mesh(geometry, base);
    mesh.castShadow = !c.display_only;
    mesh.receiveShadow = true;
    mesh.name = c.id;
    mesh.userData.componentId = c.id;
    this.root.add(mesh);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 32), this.edgeMaterials.base);
    edges.visible = this.edgesOn;
    edges.userData.componentId = c.id;
    this.edgeGroup.add(edges);
    const triangles = geometry.index ? geometry.index.count / 3 : geometry.attributes.position.count / 3;
    // geometry keeps references to the exact incoming buffers for the next exact comparison
    const rec = {
      id: c.id, mesh, edges, base, ghost: false, flashStart: 0, flashMaterial: null, triangles,
      geometry: { positions: c.positions, normals: c.normals, indices: c.indices }, appearance: appearanceKey(c), ...this._meta(c),
    };
    mesh.userData.record = rec;
    return rec;
  }

  _disposeRecord(rec) {
    this.root.remove(rec.mesh);
    this.edgeGroup.remove(rec.edges);
    rec.mesh.geometry.dispose();
    rec.edges.geometry.dispose();
    if (rec.flashMaterial) rec.flashMaterial.dispose();
    this.flashing.delete(rec);
  }

  _baseMaterial(c) {
    const m = c.material || {};
    const translucent = !!c.display_only || (c.status && c.status !== 'current');
    const key = `${m.color}|${m.metalness}|${m.roughness}|${translucent}`;
    let material = this.materials.get(key);
    if (material) return material;
    const params = {
      color: new THREE.Color(typeof m.color === 'string' ? m.color : '#8a9198'),
      metalness: clamp01(m.metalness, 0.2),
      roughness: clamp01(m.roughness, 0.5),
      envMapIntensity: 1.0,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
    };
    // Low-metalness finishes read as coated surfaces; this is appearance only, not a material claim.
    material = params.metalness < 0.5
      ? new THREE.MeshPhysicalMaterial({ ...params, clearcoat: 0.25, clearcoatRoughness: 0.38 })
      : new THREE.MeshStandardMaterial(params);
    if (translucent) {
      material.transparent = true;
      material.opacity = 0.7;
    }
    this.materials.set(key, material);
    return material;
  }

  _variant(base, kind) {
    const key = `${base.uuid}|${kind}`;
    let material = this.variants.get(key);
    if (material) return material;
    material = base.clone();
    if (kind === 'selected') {
      material.emissive = AMBER.clone();
      material.emissiveIntensity = 0.38;
    } else if (kind === 'hover') {
      material.emissive = AMBER.clone();
      material.emissiveIntensity = 0.16;
    } else if (kind === 'ghost') {
      material.color = material.color.clone().lerp(GHOST_TINT, 0.6);
      material.transparent = true;
      material.opacity = 0.16;
      material.depthWrite = false;
    }
    this.variants.set(key, material);
    return material;
  }

  _refreshLooks() {
    for (const rec of this.components.values()) this._applyLook(rec);
    this._updateMarkers();
    this.dirty = true;
  }

  _applyLook(rec) {
    const selected = this.selected.has(rec.id);
    let material = rec.base;
    if (rec.ghost || rec.pipeFaded) material = this._variant(rec.base, 'ghost');
    else if (selected) material = this._variant(rec.base, rec.sectioned ? 'hover' : 'selected'); // a selected section's sleeve carries the emphasis
    else if (this.hovered === rec) material = this._variant(rec.base, 'hover');
    else if (rec.flashMaterial) material = rec.flashMaterial;
    rec.mesh.material = material;
    rec.mesh.castShadow = !rec.ghost && !rec.displayOnly;
    rec.edges.material = rec.ghost ? this.edgeMaterials.ghost : selected ? this.edgeMaterials.selected : this.edgeMaterials.base;
    rec.edges.visible = this.edgesOn || selected;
  }

  _applyLabels(labels, final) {
    const incoming = new Map();
    for (const l of Array.isArray(labels) ? labels : []) {
      if (l && typeof l.semantic_id === 'string' && Array.isArray(l.anchor_m) && l.anchor_m.length === 3) incoming.set(l.semantic_id, l);
    }
    for (const [id, entry] of this.labels) {
      if (incoming.has(id)) continue;
      if (final) {
        entry.el.remove();
        this.labels.delete(id);
      } else {
        entry.ghost = true;
        entry.el.classList.add('ghost');
      }
    }
    for (const [id, l] of incoming) {
      let entry = this.labels.get(id);
      if (!entry) {
        const el = document.createElement('div');
        el.className = 'lbl';
        el.dataset.semanticId = id;
        if (this._labelsYield) el.style.pointerEvents = 'none';
        el.addEventListener('click', (event) => {
          event.stopPropagation();
          if (this.cb.onPick) this.cb.onPick({ semanticId: id, componentId: null, additive: event.shiftKey || event.metaKey });
        });
        this.labelLayer.append(el);
        entry = { el, w: 0, h: 0 };
        this.labels.set(id, entry);
      }
      entry.anchor = new THREE.Vector3(...l.anchor_m);
      entry.kind = l.kind;
      entry.ghost = false;
      entry.el.classList.remove('ghost');
      entry.el.classList.toggle('conn', l.kind === 'connection');
      entry.el.classList.toggle('not-current', l.status !== 'current');
      if (entry.text !== l.text) {
        entry.text = l.text;
        entry.el.textContent = l.text;
        entry.w = 0;
      }
      entry.el.title = `${l.text} · ${id}${l.status && l.status !== 'current' ? ` · ${l.status}` : ''}`;
    }
  }

  _updateBounds(bounds, final) {
    let min;
    let max;
    if (bounds && Array.isArray(bounds.min) && Array.isArray(bounds.max)) {
      min = bounds.min.slice();
      max = bounds.max.slice();
    } else {
      const box = new THREE.Box3();
      for (const rec of this.components.values()) box.expandByObject(rec.mesh);
      if (box.isEmpty()) return;
      min = box.min.toArray();
      max = box.max.toArray();
    }
    if (!final && this.bounds) { // previews only grow the studio, so the floor does not jump
      min = min.map((v, i) => Math.min(v, this.bounds.min[i]));
      max = max.map((v, i) => Math.max(v, this.bounds.max[i]));
    }
    const same = this.bounds && min.every((v, i) => v === this.bounds.min[i]) && max.every((v, i) => v === this.bounds.max[i]);
    this.bounds = { min, max };
    if (!same) {
      this.studio.setBounds(min, max);
      if (this.coordinatesOn) this._buildCoordinates();
    }
  }

  clear() {
    for (const rec of this.components.values()) this._disposeRecord(rec);
    this.components.clear();
    for (const entry of this.labels.values()) entry.el.remove();
    this.labels.clear();
    this._clearGhosts();
    this.pickables = [];
    this.hasScene = false;
    this.framed = false;
    this.dirty = true;
  }

  // ---------------------------------------------------------------- selection

  /** IDs of components representing one semantic ID in the scene currently shown. */
  componentsFor(semanticId) {
    const out = [];
    for (const rec of this.components.values()) if (rec.semanticId === semanticId) out.push(rec.id);
    const port = this.ports.get(semanticId);
    if (port && port.component_id && this.components.has(port.component_id)) out.push(port.component_id);
    return out;
  }

  hasComponent(id) {
    return this.components.has(id);
  }

  /**
   * componentIds: Set of component IDs to highlight; portIds: selected ports to mark; semanticIds
   * (optional): the selected semantic IDs, so coordinates and Move resolve the selected equipment
   * owner even when it has no geometry here (unplaced or deleted items). Without them the owner is
   * derived from the highlighted components and ports.
   */
  setSelection(componentIds, portIds = [], semanticIds = null) {
    this.selected = new Set(componentIds);
    this.selectedPorts = portIds;
    this.selectedIds = Array.isArray(semanticIds) ? semanticIds.slice() : null;
    const local = this.moveLocal;
    if (local && local.stage === 'drag') {
      const owner = this._owner();
      if (owner.reason || owner.entityId !== local.entityId) this._cancelLocal('selection-changed');
    }
    const pipe = this.pipeLocal;
    if (pipe && !(this.selectedIds && this.selectedIds.length === 1 && this.selectedIds[0] === pipe.connectionId)) this._pipeAbort('selection-changed');
    this._refreshLooks();
    this._syncGizmo();
    this._updateReadout();
    this._syncPipe();
  }

  _updateMarkers() {
    for (const child of [...this.markers.children]) {
      this.markers.remove(child);
      child.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
    }
    for (const id of this.selectedPorts || []) {
      const port = this.ports.get(id);
      if (!port || !Array.isArray(port.position_m) || !Array.isArray(port.direction)) continue;
      const origin = new THREE.Vector3(...port.position_m);
      const dir = new THREE.Vector3(...port.direction).normalize();
      const size = this.bounds ? Math.max(0.12, Math.hypot(...this.bounds.max.map((v, i) => v - this.bounds.min[i])) * 0.05) : 0.15;
      const arrow = new THREE.ArrowHelper(dir, origin, size, 0xe8a317, size * 0.32, size * 0.2);
      arrow.traverse((o) => { if (o.material) { o.material.depthTest = false; o.material.transparent = true; } o.renderOrder = 10; });
      arrow.userData.portId = id;
      arrow.userData.base = origin.clone();
      const preview = this.portPreview.get(id);
      if (preview) arrow.position.fromArray(preview);
      this.markers.add(arrow);
    }
    this.dirty = true;
  }

  // ---------------------------------------------------------------- camera

  _contentBox(ids) {
    const box = new THREE.Box3();
    for (const rec of this.components.values()) {
      if (ids && !ids.has(rec.id)) continue;
      if (!ids && rec.ghost) continue;
      box.expandByObject(rec.mesh);
    }
    if (box.isEmpty() && this.bounds) box.set(new THREE.Vector3(...this.bounds.min), new THREE.Vector3(...this.bounds.max));
    return box;
  }

  _fit(box, direction, margin, animate) {
    if (box.isEmpty()) return;
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(0.03, box.getSize(new THREE.Vector3()).length() / 2);
    const vfov = THREE.MathUtils.degToRad(this.camera.fov);
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * this.camera.aspect);
    const distance = (radius / Math.sin(Math.min(vfov, hfov) / 2)) * margin;
    const position = center.clone().addScaledVector(direction.clone().normalize(), distance);
    this.camera.near = Math.max(0.002, distance / 400);
    this.camera.far = distance * 40 + 40;
    this.camera.updateProjectionMatrix();
    if (!animate) {
      this.camera.position.copy(position);
      this.controls.target.copy(center);
      this.controls.update();
      this.dirty = true;
      return;
    }
    this.tween = {
      start: performance.now(), duration: 420,
      fromPos: this.camera.position.clone(), toPos: position,
      fromTarget: this.controls.target.clone(), toTarget: center,
    };
    this.dirty = true;
  }

  _dragging() {
    return !!(this.moveLocal && this.moveLocal.stage === 'drag') || !!(this.pipeLocal && this.pipeLocal.stage === 'drag');
  }

  setView(name, animate = true) {
    if (this._dragging()) return; // the camera stays put while a gizmo drag defines its plane
    const dir = VIEW_DIRECTIONS[name] || VIEW_DIRECTIONS.iso;
    this._fit(this._contentBox(), new THREE.Vector3(...dir), name === 'top' || name === 'front' || name === 'right' ? 1.02 : 1.0, animate);
  }

  fitAll(animate = true) {
    if (this._dragging()) return;
    const dir = this.camera.position.clone().sub(this.controls.target);
    this._fit(this._contentBox(), dir.lengthSq() > 0 ? dir : new THREE.Vector3(...VIEW_DIRECTIONS.iso), 1.0, animate);
  }

  focusSelection(animate = true) {
    if (this._dragging()) return false;
    const ids = new Set([...this.selected].filter((id) => this.components.has(id)));
    if (!ids.size) return false;
    const dir = this.camera.position.clone().sub(this.controls.target);
    this._fit(this._contentBox(ids), dir, 1.25, animate);
    return true;
  }

  cameraState() {
    return { position: this.camera.position.toArray(), target: this.controls.target.toArray() };
  }

  // ---------------------------------------------------------------- display options

  setEdges(on) {
    this.edgesOn = !!on;
    this._refreshLooks();
  }

  setLabelMode(mode) {
    this.labelMode = mode;
    this.labelLayer.classList.toggle('off', mode === 'off');
    this.dirty = true;
  }

  // ---------------------------------------------------------------- coordinates

  /**
   * Show or hide this view's world coordinates: origin, labelled +X/+Y/+Z axes and an XY reference
   * grid at Z = 0 (metres, Z up; the studio's own floor grid is hidden meanwhile so the two never
   * conflict), and the selected equipment origin readout. Display only: nothing is written.
   */
  setCoordinates(on) {
    const next = !!on;
    if (next === this.coordinatesOn) return;
    this.coordinatesOn = next;
    this.studio.setReferencePlane(next);
    if (next) this._buildCoordinates();
    else this._disposeCoordinates();
    this._updateReadout();
    this.dirty = true;
  }

  _buildCoordinates() {
    const extent = gridExtent(this.bounds);
    const key = JSON.stringify(extent);
    if (this.coord && this.coord.key === key) return;
    this._disposeCoordinates();
    const group = new THREE.Group();
    group.name = 'world-coordinates';
    const disposables = [];
    const segments = (points, color, opacity, renderOrder) => {
      if (!points.length) return;
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3));
      const material = new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
      const lines = new THREE.LineSegments(geometry, material);
      lines.renderOrder = renderOrder;
      group.add(lines);
      disposables.push(geometry, material);
    };
    const [x0, y0] = extent.min;
    const [x1, y1] = extent.max;
    const xs = gridLines(x0, x1, extent.minor, extent.major);
    const ys = gridLines(y0, y1, extent.minor, extent.major);
    const grid = (xv, yv) => [...xv.flatMap((x) => [x, y0, 0, x, y1, 0]), ...yv.flatMap((y) => [x0, y, 0, x1, y, 0])];
    segments(grid(xs.minor, ys.minor), 0x1b2026, 0.06, 1);
    segments(grid(xs.major, ys.major), 0x1b2026, 0.16, 1);
    segments([x0, 0, 0, x1, 0, 0], AXIS_COLORS.X, 0.8, 2);
    segments([0, y0, 0, 0, y1, 0], AXIS_COLORS.Y, 0.8, 2);
    segments([0, 0, 0, 0, 0, extent.zTop], AXIS_COLORS.Z, 0.8, 2);
    this.helpers.add(group);
    const t = extent.major;
    const label = (cls, text, title, at) => {
      const el = div(`axis-lbl ${cls}`, text);
      el.title = title;
      this.axisLayer.append(el);
      return { el, at: new THREE.Vector3(...at) };
    };
    const labels = [
      label('x', '+X', 'World +X axis · metres', [t, 0, 0]),
      label('y', '+Y', 'World +Y axis · metres', [0, t, 0]),
      label('z', '+Z', 'World +Z axis (up) · metres', [0, 0, t]),
      label('o', '0', 'World origin (0, 0, 0) m · Z up', [0, 0, 0]),
    ];
    this.coord = { group, disposables, labels, extent, key };
    this.dirty = true;
  }

  _disposeCoordinates() {
    if (!this.coord) return;
    this.helpers.remove(this.coord.group);
    for (const d of this.coord.disposables) d.dispose();
    for (const l of this.coord.labels) l.el.remove();
    this.coord = null;
    this.dirty = true;
  }

  /** Readout panel: preview, selected equipment origin with its source, or what to select. */
  _updateReadout() {
    if (this.disposed) return;
    const show = this.coordinatesOn || this.moveEnabled || !!this.preview;
    if (!show) {
      this.readout = null;
      this._readoutKey = null;
      this.readoutEl.hidden = true;
      return;
    }
    const r = coordinateReadout({
      context: this.context, viewerSource: this.source || null, owner: this._owner(), preview: this.preview,
      move: { enabled: this.moveEnabled, eligible: this.moveGate, snap: this.moveSnap }, section: this._sectionReadout(),
    });
    r.world = WORLD_NOTE;
    this.readout = r;
    const key = JSON.stringify(r);
    if (key === this._readoutKey) return;
    this._readoutKey = key;
    const xyz = div('cr-xyz');
    if (r.xyz) {
      r.xyz.forEach((v, i) => {
        const axis = 'XYZ'[i];
        xyz.append(Object.assign(document.createElement('span'), { className: `ax ${axis.toLowerCase()}`, textContent: axis }),
          Object.assign(document.createElement('span'), { className: 'v', textContent: formatMetres(v) }));
      });
      xyz.append(Object.assign(document.createElement('span'), { className: 'u', textContent: 'm' }));
    } else {
      xyz.textContent = r.kind === 'unplaced' ? 'Unplaced' : '—';
    }
    this.readoutEl.replaceChildren(div('cr-title', r.title), xyz, ...r.lines.map((l) => div('cr-line', l)), div('cr-world', WORLD_NOTE));
    this.readoutEl.dataset.kind = r.kind;
    this.readoutEl.hidden = false;
  }

  // ---------------------------------------------------------------- engineering context and move

  /**
   * Authoritative context from the application, replaced on every call:
   * project: the current model exactly as the service returns it (state.project); inputDigest: the
   * current document digest (state.document.input_digest), without which Move stays disabled;
   * online: boolean; mode: this pane's 'live' | 'published'; installedSource: the pane loader's
   * installed {url, src}; move: the application's global move record or null. Cheap to call repeatedly.
   */
  setEngineeringContext(ctx) {
    this.context = ctx && typeof ctx === 'object' ? {
      project: ctx.project || null,
      inputDigest: typeof ctx.inputDigest === 'string' && ctx.inputDigest ? ctx.inputDigest : null,
      online: ctx.online === true,
      mode: ctx.mode === 'live' || ctx.mode === 'published' ? ctx.mode : null,
      installedSource: ctx.installedSource || null,
      move: ctx.move || null,
      section: ctx.section || null,
      pipeMove: ctx.pipeMove || null,
      pipeCapable: ctx.pipeCapable === true,
      paneIndex: Number.isInteger(ctx.paneIndex) ? ctx.paneIndex : null,
    } : null;
    if (this.moveLocal && this.moveLocal.stage === 'committed') this.moveLocal = null; // the record describes it now
    this._reconcileLocal();
    this._applyPreview();
    this._syncGizmo();
    this._updateReadout();
    this._syncPipe();
  }

  /** Turn the Move tool on or off (off by default; ordinary select/orbit otherwise). */
  setMoveEnabled(on) {
    const next = !!on;
    if (next === this.moveEnabled) return;
    this.moveEnabled = next;
    if (next) this._ensureTransform();
    else {
      this._cancelLocal('move-disabled');
      this._pipeAbort('move-disabled');
    }
    this._syncGizmo();
    this._updateReadout();
    this._syncPipe();
  }

  /** Snap step of the world delta in metres (default 0.05), or null for off. */
  setMoveSnap(stepMetresOrNull) {
    this.moveSnap = normalizeSnap(stepMetresOrNull);
    this._updateReadout();
  }

  /**
   * Cancel this view's unsubmitted gizmo preview (Escape, tool changes). Emits onMoveCancel(reason)
   * once and returns true when something was cancelled; a submitted move cannot be revoked here.
   */
  cancelMove(reason = 'cancelled') {
    return this._cancelLocal(String(reason));
  }

  /** Read-only state of the Move tool for pane tools and automated checks. */
  moveSnapshot() {
    const gate = this.moveGate || this._eligibility();
    const local = this.moveLocal;
    const tc = this.transform;
    const move = this.context && this.context.move;
    const copy = (v) => (Array.isArray(v) ? v.slice() : v ?? null);
    return {
      enabled: this.moveEnabled,
      snap: this.moveSnap,
      eligible: {
        ok: gate.ok, reason: gate.reason, message: gate.message, entityId: gate.entityId, tag: gate.tag,
        base: copy(gate.base), rotation: copy(gate.rotation), sourceRevision: gate.sourceRevision, inputDigest: gate.inputDigest,
        intent: gate.intent ? { ...gate.intent, position: gate.intent.position.slice() } : null,
      },
      active: local ? {
        entityId: local.entityId, stage: local.stage, axis: local.axis, base: local.base.slice(), position: local.position.slice(),
        delta: local.position.map((v, i) => v - local.base[i]), sourceRevision: local.sourceRevision, inputDigest: local.inputDigest,
      } : null,
      preview: this.preview ? { ...this.preview, base: this.preview.base.slice(), position: this.preview.position.slice(), delta: this.preview.delta.slice() } : null,
      record: move ? {
        paneIndex: move.paneIndex ?? null, entityId: move.entityId, sourceRevision: move.sourceRevision, inputDigest: move.inputDigest,
        basePosition: copy(move.basePosition), position: copy(move.position), rotation: copy(move.rotation), phase: move.phase,
      } : null,
      gizmo: { attached: !!(tc && tc.object), enabled: !!(tc && tc.enabled), dragging: !!(tc && tc.dragging), axis: tc ? tc.axis : null },
      orbitEnabled: this.controls.enabled,
      orbitHeld: !!this._orbitHold,
    };
  }

  _owner() {
    const componentOwners = [];
    for (const id of this.selected) {
      const rec = this.components.get(id);
      if (rec) componentOwners.push(rec.semanticId);
    }
    const portOwners = (this.selectedPorts || []).map((id) => this.ports.get(id)).filter(Boolean).map((p) => p.entity_id);
    return selectionOwner({ ids: this.selectedIds, componentOwners, portOwners }, this.context ? this.context.project : null);
  }

  _ownerScene(entityId) {
    const out = { components: 0, physical: 0 };
    if (!entityId) return out;
    for (const rec of this.components.values()) {
      if (rec.ghost || rec.semanticId !== entityId) continue;
      out.components += 1;
      if (!rec.displayOnly) out.physical += 1;
    }
    return out;
  }

  _eligibility() {
    const owner = this._owner();
    return moveEligibility({
      context: this.context, viewerSource: this.source || null, owner, scene: this._ownerScene(owner.entityId), local: this.moveLocal,
    });
  }

  _ensureTransform() {
    if (this.transform) return this.transform;
    const tc = new TransformControls(this.camera, this.canvas);
    tc.setMode('translate');
    tc.setSpace('world');
    tc.setSize(0.9);
    tc.enabled = false; // snapping is applied to the world delta (equipment-move.snapPosition)
    const helper = tc.getHelper();
    helper.name = 'move-gizmo';
    helper.traverse((o) => {
      if (o.name === 'XYZ') o.layers.set(EXCLUDED_LAYER); // no free screen-plane drag: world axes and planes only
      const color = o.material && o.material.color;
      if (color && GIZMO_TINT[color.getHex()] !== undefined) color.setHex(GIZMO_TINT[color.getHex()]);
    });
    tc.addEventListener('dragging-changed', (e) => {
      this.controls.enabled = !e.value && !this._orbitHold;
      this.dirty = true;
    });
    tc.addEventListener('change', () => { this.dirty = true; });
    tc.addEventListener('mouseDown', () => this._gizmoDown());
    tc.addEventListener('objectChange', () => this._gizmoChange());
    tc.addEventListener('mouseUp', () => this._gizmoUp());
    this.scene.add(helper);
    this.transform = tc;
    const lbl = (axis) => {
      const el = div(`gizmo-lbl ${axis.toLowerCase()}`, axis);
      el.title = `Drag along world ${axis} (metres)`;
      el.style.visibility = 'hidden';
      this.axisLayer.append(el);
      return el;
    };
    this.gizmoLabels = { X: lbl('X'), Y: lbl('Y'), Z: lbl('Z') };
    return tc;
  }

  /** Attach the gizmo to the selected item's origin when it may move here; detach otherwise. */
  _syncGizmo() {
    if (this.disposed) return;
    const gate = this._eligibility();
    this.moveGate = gate;
    const key = `${gate.ok}|${gate.reason}|${gate.entityId}|${gate.sourceRevision}|${gate.inputDigest}`;
    const tc = this.transform;
    const local = this.moveLocal;
    if (tc && !(local && (local.stage === 'begin' || local.stage === 'drag'))) {
      const attach = this.moveEnabled && gate.ok && this.active;
      if (attach) {
        this.moveProxy.position.fromArray(gate.base);
        this.moveProxy.updateMatrixWorld(true);
        if (tc.object !== this.moveProxy) tc.attach(this.moveProxy);
        tc.enabled = true;
      } else {
        if (tc.object) tc.detach();
        tc.enabled = false;
        this._yieldLabels(false);
      }
      const gizmoKey = attach ? gate.base.join(',') : '';
      if (gizmoKey !== this._gizmoKey) {
        this._gizmoKey = gizmoKey;
        this.dirty = true;
      }
    }
    if (key !== this._gateKey && !this._disposing) {
      this._gateKey = key;
      if (this.cb.onMoveAvailability) {
        this.cb.onMoveAvailability({ ok: gate.ok, reason: gate.reason, message: gate.message, entityId: gate.entityId, tag: gate.tag });
      }
    }
  }

  _gizmoDown() {
    this._gizmoGesture = true;
    if (this._aborting || this.disposed) return;
    const gate = this._eligibility();
    if (!gate.ok || !this.moveEnabled) {
      this._gizmoRelease();
      this._syncGizmo();
      return;
    }
    const local = {
      entityId: gate.entityId, tag: gate.tag, base: gate.base.slice(), rotation: gate.rotation.slice(),
      sourceRevision: gate.sourceRevision, inputDigest: gate.inputDigest, url: this.source.url,
      position: gate.base.slice(), stage: 'begin', axis: this.transform.axis, recordSeen: false,
    };
    this.moveLocal = local;
    let accepted = false;
    try {
      accepted = typeof this.cb.onMoveBegin === 'function' && this.cb.onMoveBegin({
        entityId: local.entityId, position: local.base.slice(), sourceRevision: local.sourceRevision, inputDigest: local.inputDigest,
      }) === true;
    } catch (error) {
      console.error(error);
      accepted = false;
    }
    if (this.disposed || this.moveLocal !== local) return; // cancelled or replaced during the callback
    if (!accepted) { // refused: no preview, nothing to cancel
      this.moveLocal = null;
      this._gizmoRelease();
      this._syncGizmo();
      this._applyPreview();
      return;
    }
    local.stage = 'drag';
    this.canvas.style.cursor = 'grabbing';
    this._escapeOn();
    const again = this._eligibility(); // the application may have pushed a new context meanwhile
    if (!again.ok || again.entityId !== local.entityId || !sameBase(again, local)) {
      this._cancelLocal(again.reason || 'stale');
      return;
    }
    this._reconcileLocal();
    this._applyPreview();
  }

  _gizmoChange() {
    const local = this.moveLocal;
    if (this._aborting || !local || local.stage !== 'drag') return;
    const next = snapPosition(local.base, this.moveProxy.position.toArray(), this.moveSnap, this.transform.axis || local.axis || 'XYZ');
    if (!next) return;
    this.moveProxy.position.fromArray(next); // the gizmo shows the snapped origin
    if (next.every((v, i) => v === local.position[i])) return;
    local.position = next;
    this._applyPreview();
    if (this.cb.onMovePreview) this.cb.onMovePreview(next.slice());
  }

  _gizmoUp() {
    const local = this.moveLocal;
    if (this._aborting || !local || local.stage !== 'drag') return;
    this._escapeOff();
    this.canvas.style.cursor = '';
    if (isNoOp(local.base, local.position)) { // released where it started: no command
      this.moveLocal = null;
      this.moveProxy.position.fromArray(local.base);
      this._applyPreview();
      this._syncGizmo();
      if (this.cb.onMoveCancel) this.cb.onMoveCancel('no-op');
      return;
    }
    local.stage = 'committed'; // drawn as submitted until the application's record arrives
    this._applyPreview();
    this._syncGizmo();
    if (this.cb.onMoveCommit) this.cb.onMoveCommit(local.position.slice());
  }

  /** End TransformControls' gesture without a commit; orbit stays suspended until that pointer is released. */
  _gizmoRelease(tc = this.transform) {
    if (!tc) return;
    if (this._pointerDown != null) this._holdOrbitUntilRelease();
    this._aborting = true;
    try {
      if (tc.dragging) tc.pointerUp(null);
    } finally {
      this._aborting = false;
    }
    tc.disconnect(); // drops the aborted gesture's pointermove listener
    tc.connect();
    if (!this._orbitHold) this.controls.enabled = true;
    this.canvas.style.cursor = '';
    this.dirty = true;
  }

  /** Keep OrbitControls off while the cancelled gesture's pointer is still pressed (no camera jump). */
  _holdOrbitUntilRelease() {
    this._orbitHold = true;
    this.controls.enabled = false;
    if (this._orbitHoldOff) return;
    const off = () => {
      window.removeEventListener('pointerup', off, true);
      window.removeEventListener('pointercancel', off, true);
      this._orbitHoldOff = null;
      this._orbitHold = false;
      if (!this.disposed && !(this.transform && this.transform.dragging) && !(this.pipeTransform && this.pipeTransform.dragging)) this.controls.enabled = true;
    };
    this._orbitHoldOff = off;
    window.addEventListener('pointerup', off, true);
    window.addEventListener('pointercancel', off, true);
  }

  _escapeOn() {
    if (this._escapeBound) return;
    this._onEscape = this._onEscape || ((e) => {
      if (e.key !== 'Escape' || !(this.moveLocal && this.moveLocal.stage === 'drag')) return;
      e.preventDefault();
      e.stopImmediatePropagation(); // one Escape cancels the drag only
      this._cancelLocal('escape');
    });
    window.addEventListener('keydown', this._onEscape, true);
    this._escapeBound = true;
  }

  _escapeOff() {
    if (!this._escapeBound) return;
    window.removeEventListener('keydown', this._onEscape, true);
    this._escapeBound = false;
  }

  /** Cancel this view's unsubmitted gesture: restore, release the gizmo and orbit, emit onMoveCancel once. */
  _cancelLocal(reason) {
    const local = this.moveLocal;
    if (!local || local.stage === 'committed') return false;
    this.moveLocal = null;
    this._escapeOff();
    this._gizmoRelease();
    this.moveProxy.position.fromArray(local.base);
    this._applyPreview();
    this._syncGizmo();
    this._updateReadout();
    if (this.cb.onMoveCancel) this.cb.onMoveCancel(reason);
    return true;
  }

  /** An unsubmitted drag ends when its base, source, connection or record no longer holds. */
  _reconcileLocal() {
    const local = this.moveLocal;
    if (!local || local.stage !== 'drag') return;
    const reason = dragEndReason(this.context, local, this.source || null);
    if (reason) {
      this._cancelLocal(reason);
      return;
    }
    if (this.context.move) local.recordSeen = true;
  }

  _sceneChangedForMove() {
    const local = this.moveLocal;
    if (local) {
      const same = this.source.final && this.source.url === local.url
        && this.source.source_revision === local.sourceRevision && this.source.input_digest === local.inputDigest;
      if (!same) {
        if (local.stage === 'committed') this.moveLocal = null;
        else this._cancelLocal('source-changed');
      }
    }
    this._applyPreview(); // records may be new objects: offsets and base ghosts follow
    this._syncGizmo();
    this._updateReadout();
    if (this.pipeLocal && !(this.source.final && this.source.url === this.pipeLocal.url)) this._pipeAbort('source-changed');
    this._syncPipe();
  }

  /**
   * Draw the effective preview: every owner component mesh and its feature edges are offset by the
   * world delta (vertex buffers untouched), labels and selected-port markers follow, and the base
   * position stays visible as a faint ghost. Everything else is reset to zero offset.
   */
  _applyPreview() {
    if (this.disposed) return;
    const p = effectivePreview({
      local: this.moveLocal, move: this.context ? this.context.move : null, viewerSource: this.source || null,
      mode: this.context ? this.context.mode : null,
    });
    const previous = this.preview;
    this.preview = p;
    const id = p ? p.entityId : null;
    const delta = p ? p.delta : [0, 0, 0];
    const moved = !!p && delta.some((d) => d !== 0);
    for (const rec of this.components.values()) {
      const on = id !== null && rec.semanticId === id;
      rec.mesh.position.set(...(on ? delta : [0, 0, 0]));
      rec.edges.position.set(...(on ? delta : [0, 0, 0]));
      rec.preview = on;
    }
    for (const [labelId, entry] of this.labels) {
      const on = labelId === id;
      entry.offset = on ? delta.slice() : null;
      const phase = on ? p.phase : null;
      if ((entry.el.dataset.preview || null) === phase) continue;
      entry.el.classList.toggle('preview', on);
      if (on) entry.el.dataset.preview = phase;
      else delete entry.el.dataset.preview;
      entry.w = 0; // the phase suffix changes the label's size: measure again for the layout
    }
    this.portPreview = new Map();
    for (const port of this.ports.values()) {
      if (id !== null && port.entity_id === id && isFiniteVec3(port.position_m)) this.portPreview.set(port.id, applyDelta(port.position_m, delta));
    }
    for (const arrow of this.markers.children) {
      const at = this.portPreview.get(arrow.userData.portId);
      if (at) arrow.position.fromArray(at);
      else if (arrow.userData.base) arrow.position.copy(arrow.userData.base);
    }
    this._syncGhosts(moved ? id : null);
    const same = previous && p && previous.entityId === p.entityId && previous.delta.every((d, i) => d === p.delta[i]);
    if (!same && (previous || p)) this.studio.invalidate(); // contact shadow and shadow map follow the preview
    const key = p ? `${p.entityId}|${p.delta.join(',')}|${p.phase}` : '';
    if (key !== this._previewKey) { // repeated identical contexts (state polling) need no new frame
      this._previewKey = key;
      this.dirty = true;
    }
    this._updateReadout();
  }

  _syncGhosts(entityId) {
    const recs = entityId ? [...this.components.values()].filter((r) => !r.ghost && r.semanticId === entityId) : [];
    const key = recs.map((r) => r.mesh.geometry.uuid).join(',');
    if (key === this._ghostKey) return;
    this._clearGhosts();
    this._ghostKey = key;
    if (!recs.length) return;
    if (!this.ghostMaterial) {
      this.ghostMaterial = new THREE.MeshBasicMaterial({ color: 0x8a939b, transparent: true, opacity: 0.2, depthWrite: false });
    }
    for (const rec of recs) {
      const ghost = new THREE.Mesh(rec.mesh.geometry, this.ghostMaterial); // shares the installed geometry at the base origin
      ghost.name = `${rec.id}@base`;
      ghost.renderOrder = 1;
      this.ghostGroup.add(ghost);
    }
  }

  _clearGhosts() {
    for (const ghost of [...this.ghostGroup.children]) this.ghostGroup.remove(ghost); // geometry belongs to its record
    this._ghostKey = '';
  }

  // ---------------------------------------------------------------- straight pipe sections

  /** Whether this view shows exactly the current model (final scene, same revision and digest). */
  _showsCurrent() {
    const ctx = this.context;
    const src = this.source;
    return !!(ctx && ctx.project && src && src.final && ctx.inputDigest
      && src.source_revision === ctx.project.revision && src.input_digest === ctx.inputDigest);
  }

  /**
   * The straight section under a hit point of a line's pipe sweep, from the current authoritative
   * route, or null (another component, a bend, an older or unfinished scene, a route not current).
   */
  _sectionAt(rec, point) {
    const ctx = this.context;
    if (!rec || rec.key !== 'pipe' || rec.ghost || !point || !this._showsCurrent()) return null;
    const r = routeOf(ctx.project, rec.semanticId);
    if (!r || r.route.status !== 'current') return null;
    const index = sectionAtPoint(straightSections(ctx.project, rec.semanticId), point, pipeRadius(r.connection));
    return index === null ? null : {
      connectionId: rec.semanticId, segmentIndex: index, sourceRevision: ctx.project.revision, inputDigest: ctx.inputDigest,
      routeKey: routeKey(ctx.project, rec.semanticId),
    };
  }

  /** Whether the selected section may be moved here: {ok, reason, message, connectionId, segmentIndex, section, intent}. */
  _pipeEligibility() {
    const ctx = this.context;
    const sec = ctx ? ctx.section : null;
    const named = sec ? { connectionId: sec.connectionId, segmentIndex: sec.segmentIndex } : {};
    const result = (reason, extra = {}) => ({
      ok: !reason, reason, message: reason ? (reason === 'none' ? 'Pick a straight run of the line, or choose a section in the list.' : PIPE_START_REASONS[reason] || MOVE_REASONS[reason] || reason) : null,
      connectionId: null, segmentIndex: null, section: null, intent: null, ...named, ...extra,
    });
    if (!ctx || !ctx.project) return result('no-context');
    if (!sec) return result('none');
    if (!ctx.pipeCapable) return result('unsupported');
    const gate = sourceGate(ctx, this.source || null);
    if (!gate.ok) return result(gate.reason);
    const revision = ctx.project.revision;
    if (sec.sourceRevision !== revision || sec.inputDigest !== ctx.inputDigest) return result('stale');
    const r = routeOf(ctx.project, sec.connectionId);
    if (!r) return result('no-route');
    if (r.route.status !== 'current') return result('route-not-current');
    if (routeKey(ctx.project, sec.connectionId) !== sec.routeKey) return result('route-changed');
    const section = straightSections(ctx.project, sec.connectionId).find((x) => x.index === sec.segmentIndex);
    if (!section) return result('no-section');
    // outward port frames of this scene (the same revision): the preview keeps both ends on them
    const from = this.ports.get(r.connection.from_port_id);
    const to = this.ports.get(r.connection.to_port_id);
    if (!from || !to || !isFiniteVec3(from.direction) || !isFiniteVec3(to.direction)) return result('no-frames', { section });
    if (ctx.move) return result('equipment-busy', { section });
    const pm = ctx.pipeMove;
    if (pm) {
      const ours = pm.phase === 'preview' && pm.connectionId === sec.connectionId && pm.segmentIndex === sec.segmentIndex
        && pm.sourceRevision === revision && pm.inputDigest === ctx.inputDigest && (ctx.paneIndex === null || pm.paneIndex === ctx.paneIndex);
      if (!ours) return result(pm.phase === 'uncertain' ? 'unconfirmed' : 'busy', { section });
    }
    return result(null, {
      section, intent: {
        connectionId: sec.connectionId, segmentIndex: sec.segmentIndex, sourceRevision: revision, inputDigest: ctx.inputDigest, routeKey: sec.routeKey,
        frames: { start: from.direction.slice(), end: to.direction.slice() },
      },
    });
  }

  /** The application's section record when it was drawn on exactly this view's scene (Live, final). */
  _pipeRecordHere() {
    const ctx = this.context;
    const pm = ctx ? ctx.pipeMove : null;
    const src = this.source;
    if (!pm || !src || !src.final || ctx.mode !== 'live') return null;
    return pm.sourceRevision === src.source_revision && pm.inputDigest === src.input_digest ? pm : null;
  }

  _ensurePipeTransform() {
    if (this.pipeTransform) return this.pipeTransform;
    const tc = new TransformControls(this.camera, this.canvas);
    tc.setMode('translate');
    tc.setSpace('world');
    tc.setSize(0.8);
    tc.enabled = false;
    const helper = tc.getHelper();
    helper.name = 'pipe-section-gizmo';
    helper.traverse((o) => {
      // single world axes only: no plane or free handles (a section moves along exactly one axis)
      if (['XYZ', 'XY', 'YZ', 'XZ', 'XYZX', 'XYZY', 'XYZZ'].includes(o.name)) o.layers.set(EXCLUDED_LAYER);
      const color = o.material && o.material.color;
      if (color && GIZMO_TINT[color.getHex()] !== undefined) color.setHex(GIZMO_TINT[color.getHex()]);
    });
    tc.addEventListener('dragging-changed', (e) => {
      this.controls.enabled = !e.value && !this._orbitHold;
      this.dirty = true;
    });
    tc.addEventListener('change', () => { this.dirty = true; });
    tc.addEventListener('mouseDown', () => this._pipeDown());
    tc.addEventListener('objectChange', () => this._pipeChange());
    tc.addEventListener('mouseUp', () => this._pipeUp());
    this.scene.add(helper);
    this.pipeTransform = tc;
    const lbl = (axis) => {
      const el = div(`gizmo-lbl ${axis.toLowerCase()}`, axis);
      el.title = `Drag the section along world ${axis} (metres)`;
      el.style.visibility = 'hidden';
      this.axisLayer.append(el);
      return el;
    };
    this.pipeGizmoLabels = { X: lbl('X'), Y: lbl('Y'), Z: lbl('Z') };
    return tc;
  }

  /** Overlay, gizmo and availability of the section tool, from the context and this view's scene. */
  _syncPipe() {
    if (this.disposed) return;
    const gate = this._pipeEligibility();
    this.pipeGate = gate;
    this._drawSection();
    const local = this.pipeLocal;
    const attach = this.moveEnabled && gate.ok && this.active && !this._dragging();
    if (!local) {
      if (attach) {
        const tc = this._ensurePipeTransform();
        const mine = this._pipeRecordHere();
        const offset = mine && mine.connectionId === gate.connectionId && mine.segmentIndex === gate.segmentIndex ? mine.offset : [0, 0, 0];
        this.pipeProxy.position.fromArray(gate.section.midpoint_m.map((v, i) => v + offset[i]));
        this.pipeProxy.updateMatrixWorld(true);
        tc.showX = gate.section.move_axes.includes(0);
        tc.showY = gate.section.move_axes.includes(1);
        tc.showZ = gate.section.move_axes.includes(2);
        if (tc.object !== this.pipeProxy) tc.attach(this.pipeProxy);
        tc.enabled = true;
      } else if (this.pipeTransform) {
        if (this.pipeTransform.object) this.pipeTransform.detach();
        this.pipeTransform.enabled = false;
      }
      this.dirty = true;
    }
    this._updateReadout();
    const key = `${gate.ok}|${gate.reason}|${gate.connectionId}|${gate.segmentIndex}`;
    if (key !== this._pipeGateKey && !this._disposing) {
      this._pipeGateKey = key;
      if (this.cb.onPipeAvailability) this.cb.onPipeAvailability({ ok: gate.ok, reason: gate.reason, message: gate.message });
    }
  }

  _pipeDown() {
    this._gizmoGesture = true;
    if (this._aborting || this.disposed) return;
    const gate = this._pipeEligibility();
    const tc = this.pipeTransform;
    const k = tc ? 'XYZ'.indexOf(tc.axis) : -1;
    if (!gate.ok || !this.moveEnabled || tc.axis.length !== 1 || !gate.section.move_axes.includes(k)) {
      this._gizmoRelease(tc);
      this._syncPipe();
      return;
    }
    const mine = this._pipeRecordHere();
    const continuing = !!mine && mine.phase === 'preview' && mine.connectionId === gate.connectionId && mine.segmentIndex === gate.segmentIndex;
    const local = {
      connectionId: gate.connectionId, segmentIndex: gate.segmentIndex, base: gate.section.midpoint_m.slice(), axis: k,
      offset: continuing ? mine.offset.slice() : [0, 0, 0], began: false, url: this.source.url, stage: 'begin',
    };
    this.pipeLocal = local;
    if (!continuing) {
      let accepted = false;
      try {
        accepted = typeof this.cb.onPipeBegin === 'function' && this.cb.onPipeBegin({ ...gate.intent }) === true;
      } catch (error) {
        console.error(error);
      }
      if (this.disposed || this.pipeLocal !== local) return;
      if (!accepted) {
        this.pipeLocal = null;
        this._gizmoRelease(tc);
        this._syncPipe();
        return;
      }
      local.began = true;
    }
    local.stage = 'drag';
    this.canvas.style.cursor = 'grabbing';
    if (!this._pipeEscapeBound) {
      this._onPipeEscape = this._onPipeEscape || ((e) => {
        if (e.key !== 'Escape' || !this.pipeLocal) return;
        e.preventDefault();
        e.stopImmediatePropagation(); // one Escape ends the drag and its unsent preview
        this._pipeAbort('escape');
      });
      window.addEventListener('keydown', this._onPipeEscape, true);
      this._pipeEscapeBound = true;
    }
  }

  _pipeChange() {
    const local = this.pipeLocal;
    const tc = this.pipeTransform;
    if (this._aborting || !local || local.stage !== 'drag' || !tc) return;
    const k = tc.axis && tc.axis.length === 1 ? 'XYZ'.indexOf(tc.axis) : local.axis;
    let d = this.pipeProxy.position.getComponent(k) - local.base[k];
    if (this.moveSnap) d = Math.round(d / this.moveSnap) * this.moveSnap;
    d = Math.max(-MAX_OFFSET_M, Math.min(MAX_OFFSET_M, d));
    d = Math.abs(d) < 1e-9 ? 0 : Math.round(d * 1e6) / 1e6;
    const offset = offsetVector(k, d);
    this.pipeProxy.position.fromArray(local.base.map((v, i) => v + offset[i])); // the gizmo shows the snapped offset on one axis
    if (offset.every((v, i) => v === local.offset[i])) return;
    local.offset = offset;
    local.axis = k;
    if (this.cb.onPipePreview) this.cb.onPipePreview(offset.slice());
  }

  /** Release ends at a reviewable preview; a zero offset of a drag that began the edit sends nothing. */
  _pipeUp() {
    const local = this.pipeLocal;
    if (this._aborting || !local || local.stage !== 'drag') return;
    this.pipeLocal = null;
    this._pipeEscapeOff();
    this.canvas.style.cursor = '';
    if (local.began && local.offset.every((v) => v === 0) && this.cb.onPipeCancel) this.cb.onPipeCancel('no-op');
    this._syncPipe();
  }

  _pipeEscapeOff() {
    if (!this._pipeEscapeBound) return;
    window.removeEventListener('keydown', this._onPipeEscape, true);
    this._pipeEscapeBound = false;
  }

  /** End this view's section drag; the application's unsent preview is cancelled too (onPipeCancel). */
  _pipeAbort(reason) {
    if (!this.cancelPipeMove(reason)) return false;
    if (this.cb.onPipeCancel) this.cb.onPipeCancel(reason);
    return true;
  }

  /** End this view's section drag only (the application already ended the record). */
  cancelPipeMove() {
    const local = this.pipeLocal;
    if (!local) return false;
    this.pipeLocal = null;
    this._pipeEscapeOff();
    this._gizmoRelease(this.pipeTransform);
    this._syncPipe();
    return true;
  }

  _clearSection() {
    for (const child of [...this.sectionGroup.children]) {
      this.sectionGroup.remove(child);
      if (child.geometry) child.geometry.dispose();
      if (child.material) child.material.dispose();
    }
    this.sectionDrawn = null;
    this._sectionKey = '';
  }

  /**
   * Display-only overlay: the selected section as an amber sleeve on a scene of the current model,
   * or, for the section record drawn on this exact scene, the tentative route (blue; amber while
   * sending or unconfirmed) with the installed pipe sweep faded. Flanges and nozzles never move.
   */
  _drawSection() {
    const ctx = this.context;
    const mine = this._pipeRecordHere();
    let highlight = null;
    let preview = null;
    let connectionId = null;
    let radius = null;
    if (mine && ctx && ctx.project) {
      const conn = ctx.project.model.connections[mine.connectionId];
      const base = conn ? straightSections({ model: { connections: { [mine.connectionId]: conn } }, physical: { routes: { [mine.connectionId]: { points_m: mine.basePoints } } } }, mine.connectionId) : [];
      const s = base.find((x) => x.index === mine.segmentIndex);
      radius = conn ? pipeRadius(conn) : null;
      connectionId = mine.connectionId;
      if (s) highlight = { start: s.start_m.map((v, i) => v + mine.offset[i]), end: s.end_m.map((v, i) => v + mine.offset[i]) };
      if (Array.isArray(mine.points)) preview = { points: mine.points.map((p) => p.slice()), phase: mine.phase };
    } else if (ctx && ctx.section && this._showsCurrent()) {
      const r = routeOf(ctx.project, ctx.section.connectionId);
      const s = r && routeKey(ctx.project, ctx.section.connectionId) === ctx.section.routeKey
        ? straightSections(ctx.project, ctx.section.connectionId).find((x) => x.index === ctx.section.segmentIndex) : null;
      if (s) {
        connectionId = ctx.section.connectionId;
        radius = pipeRadius(r.connection);
        highlight = { start: s.start_m.slice(), end: s.end_m.slice() };
      }
    }
    if (!(radius > 0)) { highlight = null; preview = null; }
    const faded = preview ? connectionId : null;
    const sectioned = highlight && !preview ? connectionId : null;
    for (const rec of this.components.values()) {
      const pipe = rec.key === 'pipe';
      const on = !!faded && pipe && rec.semanticId === faded;
      const sec = !!sectioned && pipe && rec.semanticId === sectioned;
      if (!!rec.pipeFaded !== on || !!rec.sectioned !== sec) {
        rec.pipeFaded = on;
        rec.sectioned = sec;
        this._applyLook(rec);
      }
    }
    const key = JSON.stringify([highlight, preview, radius, mine ? mine.phase : null]);
    if (key === this._sectionKey) return;
    this._clearSection();
    this._sectionKey = key;
    this.sectionDrawn = highlight || preview ? { connectionId, highlight, preview, faded: !!faded } : null;
    this.dirty = true;
    if (!highlight && !preview) return;
    const up = new THREE.Vector3(0, 1, 0);
    const rod = (a, b, r, material, name) => {
      const va = new THREE.Vector3(...a);
      const vb = new THREE.Vector3(...b);
      const len = va.distanceTo(vb);
      if (!(len > 1e-6)) return;
      const mesh = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 28, 1, true), material);
      mesh.position.copy(va).add(vb).multiplyScalar(0.5);
      mesh.quaternion.setFromUnitVectors(up, vb.clone().sub(va).normalize());
      mesh.name = name;
      mesh.renderOrder = 3;
      this.sectionGroup.add(mesh);
    };
    if (preview) {
      const amber = preview.phase === 'submitting' || preview.phase === 'uncertain';
      const tube = () => new THREE.MeshStandardMaterial({ color: amber ? 0xc98a00 : 0x2f6db5, roughness: 0.45, metalness: 0.2, transparent: true, opacity: 0.9 });
      const pts = preview.points;
      for (let i = 0; i < pts.length - 1; i += 1) rod(pts[i], pts[i + 1], radius, tube(), `preview-leg-${i}`);
      for (let i = 1; i < pts.length - 1; i += 1) {
        const joint = new THREE.Mesh(new THREE.SphereGeometry(radius, 20, 12), tube());
        joint.position.fromArray(pts[i]);
        joint.name = `preview-corner-${i}`;
        this.sectionGroup.add(joint);
      }
    }
    if (highlight) {
      const sleeve = new THREE.MeshBasicMaterial({ color: 0xd99a0b, transparent: true, opacity: preview ? 0.45 : 0.78, depthWrite: false, side: THREE.DoubleSide });
      rod(highlight.start, highlight.end, radius * 1.3, sleeve, 'section-sleeve');
    }
  }

  /** Facts of the selected (or moving) straight section for the coordinate readout, or null. */
  _sectionReadout() {
    const ctx = this.context;
    if (!ctx || !ctx.project) return null;
    const mine = this._pipeRecordHere();
    const id = mine ? mine.connectionId : ctx.section ? ctx.section.connectionId : null;
    const conn = id ? ctx.project.model.connections[id] : null;
    if (!conn) return null;
    const tag = conn.tag || id;
    if (mine) {
      const base = straightSections({ model: { connections: { [id]: conn } }, physical: { routes: { [id]: { points_m: mine.basePoints } } } }, id);
      const s = base.find((x) => x.index === mine.segmentIndex);
      if (!s) return null;
      return { tag, index: s.index, count: base.length, axis: s.axis, length: s.length_m, midpoint: s.midpoint_m, offset: mine.offset.slice(), phase: mine.phase, sourceRevision: mine.sourceRevision };
    }
    if (!this.sectionDrawn || !this.sectionDrawn.highlight) return null; // only a section this view can show
    const sections = straightSections(ctx.project, id);
    const s = sections.find((x) => x.index === ctx.section.segmentIndex);
    if (!s) return null;
    const gate = this.pipeGate || this._pipeEligibility();
    return { tag, index: s.index, count: sections.length, axis: s.axis, length: s.length_m, midpoint: s.midpoint_m, ok: gate.ok, message: gate.message, moveAxes: s.move_axes };
  }

  /** Read-only state of the section tool for the pane panel and automated checks. */
  pipeSnapshot() {
    const gate = this.pipeGate || this._pipeEligibility();
    const tc = this.pipeTransform;
    const local = this.pipeLocal;
    return {
      eligible: {
        ok: gate.ok, reason: gate.reason, message: gate.message, connectionId: gate.connectionId ?? null, segmentIndex: gate.segmentIndex ?? null,
        intent: gate.intent ? JSON.parse(JSON.stringify(gate.intent)) : null, moveAxes: gate.section ? gate.section.move_axes.slice() : [],
      },
      active: local ? { connectionId: local.connectionId, segmentIndex: local.segmentIndex, stage: local.stage, axis: local.axis, offset: local.offset.slice(), began: local.began } : null,
      gizmo: {
        attached: !!(tc && tc.object), enabled: !!(tc && tc.enabled), dragging: !!(tc && tc.dragging), axis: tc ? tc.axis : null,
        shown: tc ? ['X', 'Y', 'Z'].filter((a) => tc[`show${a}`]) : [], origin: tc && tc.object ? this.pipeProxy.position.toArray() : null,
      },
      drawn: this.sectionDrawn ? JSON.parse(JSON.stringify(this.sectionDrawn)) : null,
      faded: [...this.components.values()].filter((r) => r.pipeFaded).map((r) => r.id),
    };
  }

  /** Client coordinates of a world point in this view, or null when it is behind the camera (automated checks). */
  screenPointOfWorld(point) {
    if (!isFiniteVec3(point)) return null;
    const v = new THREE.Vector3(...point).project(this.camera);
    if (v.z < -1 || v.z > 1) return null;
    const r = this.canvas.getBoundingClientRect();
    return { x: r.left + ((v.x + 1) / 2) * r.width, y: r.top + ((1 - v.y) / 2) * r.height };
  }

  _overGizmo() {
    const over = (tc) => !!(tc && tc.enabled && tc.object && tc.axis !== null);
    return over(this.transform) || over(this.pipeTransform);
  }

  /**
   * Translate handles of the attached gizmo as last rendered: {handle, visible, hittable, world, ndc,
   * scale}. With probe, each handle's point is one that actually picks that handle (another handle
   * can cover the nominal point from some camera angles); hittable reports whether one was found.
   */
  _gizmoHandles(probe = false, tc = this.transform) {
    if (!tc || !tc.object) return [];
    const gizmo = tc.getHelper().children.find((c) => c.isTransformControlsGizmo);
    const group = gizmo && gizmo.gizmo ? gizmo.gizmo.translate : null;
    const pickers = gizmo && gizmo.picker ? gizmo.picker.translate : null;
    if (!group) return [];
    const raycaster = probe ? (this._handleRaycaster || (this._handleRaycaster = new THREE.Raycaster())) : null;
    const out = [];
    for (const [name, candidates] of HANDLE_POINTS) {
      const handle = group.children.find((c) => c.name === name);
      if (!handle) continue;
      const scale = handle.scale.x;
      const shown = handle.visible && scale > 1e-6;
      let chosen = null;
      for (const at of probe ? candidates : candidates.slice(0, 1)) {
        const world = new THREE.Vector3(...at).multiplyScalar(scale).add(handle.position);
        const ndc = world.clone().project(this.camera);
        const onScreen = ndc.z > -1 && ndc.z < 1 && Math.abs(ndc.x) <= 1 && Math.abs(ndc.y) <= 1;
        let hittable = false;
        if (probe && shown && onScreen && pickers) {
          raycaster.setFromCamera(new THREE.Vector2(ndc.x, ndc.y), this.camera);
          const first = raycaster.intersectObject(pickers, true).find((i) => i.object.visible);
          hittable = !!first && first.object.name === name;
        }
        const point = { handle: name, visible: shown && onScreen, hittable, world: world.toArray(), ndc: [ndc.x, ndc.y], scale };
        if (!chosen || (hittable && !chosen.hittable)) chosen = point;
        if (hittable || !probe) break;
      }
      out.push(chosen);
    }
    return out;
  }

  /** Whether a client point lies on a gizmo handle (updates the gizmo's hovered axis like its own hover). */
  _gizmoUnder(clientX, clientY) {
    const r = this.canvas.getBoundingClientRect();
    const under = (tc) => {
      if (!tc || !tc.enabled || !tc.object || tc.dragging) return false;
      tc.pointerHover({ x: ((clientX - r.left) / r.width) * 2 - 1, y: -((clientY - r.top) / r.height) * 2 + 1, button: -1 });
      return tc.axis !== null;
    };
    return under(this.transform) || under(this.pipeTransform);
  }

  /** Labels let pointer events through while the pointer is on a gizmo handle, so they never block a grab. */
  _yieldLabels(on) {
    if (!!this._labelsYield === on) return;
    this._labelsYield = on;
    for (const entry of this.labels.values()) entry.el.style.pointerEvents = on ? 'none' : '';
  }

  // ---------------------------------------------------------------- overlay

  _buildOverlay() {
    this.overlay = document.createElement('div');
    this.overlay.className = 'gl-overlay';
    this.leaderSvg = document.createElementNS(SVG_NS, 'svg');
    this.leaderSvg.setAttribute('class', 'leaders');
    this.labelLayer = document.createElement('div');
    this.labelLayer.className = 'label-layer';
    this.axisLayer = div('axis-layer');
    this.readoutEl = div('coord-readout');
    this.readoutEl.hidden = true;
    this.readoutEl.setAttribute('role', 'status');
    this.tooltip = document.createElement('div');
    this.tooltip.className = 'gl-tip';
    this.tooltip.hidden = true;
    this.overlay.append(this.leaderSvg, this.labelLayer, this.axisLayer, this.readoutEl, this.tooltip);
    this.host.append(this.overlay);
  }

  _updateLabels() {
    const w = this.width;
    const h = this.height;
    this.leaderSvg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    if (this.labelMode === 'off' || !this.labels.size) {
      this.leaderSvg.replaceChildren();
      for (const entry of this.labels.values()) entry.el.style.visibility = 'hidden';
      return;
    }
    const selectedSemantic = new Set();
    for (const id of this.selected) {
      const rec = this.components.get(id);
      if (rec) selectedSemantic.add(rec.semanticId);
    }
    for (const id of this.selectedPorts || []) {
      const port = this.ports.get(id);
      if (port) selectedSemantic.add(port.entity_id);
    }
    const items = [];
    const v = new THREE.Vector3();
    const anchor = new THREE.Vector3();
    for (const [id, entry] of this.labels) {
      const isSelected = selectedSemantic.has(id);
      entry.el.classList.toggle('sel', isSelected);
      if (this.labelMode === 'equipment' && entry.kind === 'connection' && !isSelected) {
        entry.el.style.visibility = 'hidden';
        continue;
      }
      if (!entry.w) {
        entry.w = entry.el.offsetWidth || 40;
        entry.h = entry.el.offsetHeight || 18;
      }
      anchor.copy(entry.anchor);
      if (entry.offset) anchor.add(v.fromArray(entry.offset)); // a move preview carries its labels along
      v.copy(anchor).project(this.camera);
      const onScreen = v.z > -1 && v.z < 1 && Math.abs(v.x) <= 1.02 && Math.abs(v.y) <= 1.02;
      items.push({
        id, ax: (v.x + 1) * 0.5 * w, ay: (1 - v.y) * 0.5 * h, w: entry.w, h: entry.h, onScreen,
        priority: (isSelected ? 4 : 0) + (entry.ghost ? -2 : 0) + (entry.kind === 'entity' ? 2 : 1),
        depth: this.camera.position.distanceTo(anchor),
      });
    }
    const placed = layoutLabels(items, { w, h }, { leader: 22 });
    const leaders = [];
    for (const p of placed) {
      const entry = this.labels.get(p.id);
      if (!p.visible) {
        entry.el.style.visibility = 'hidden';
        continue;
      }
      entry.el.style.visibility = 'visible';
      entry.el.style.transform = `translate(${Math.round(p.x)}px, ${Math.round(p.y)}px)`;
      const cls = `${entry.ghost ? 'ghost' : ''} ${selectedSemantic.has(p.id) ? 'sel' : ''}`;
      const line = document.createElementNS(SVG_NS, 'line');
      line.setAttribute('x1', p.ax.toFixed(1)); line.setAttribute('y1', p.ay.toFixed(1));
      line.setAttribute('x2', p.lx.toFixed(1)); line.setAttribute('y2', p.ly.toFixed(1));
      line.setAttribute('class', cls);
      const dot = document.createElementNS(SVG_NS, 'circle');
      dot.setAttribute('cx', p.ax.toFixed(1)); dot.setAttribute('cy', p.ay.toFixed(1)); dot.setAttribute('r', '2.2');
      dot.setAttribute('class', cls);
      leaders.push(line, dot);
    }
    for (const entry of this.labels.values()) if (!placed.some((p) => p && p.id === entry.el.dataset.semanticId)) entry.el.style.visibility = 'hidden';
    this.leaderSvg.replaceChildren(...leaders);
  }

  /** World axis labels and gizmo axis letters, projected after each rendered frame. */
  _updateAxisLabels() {
    const v = new THREE.Vector3();
    const place = (el, world, visible) => {
      v.copy(world).project(this.camera);
      const on = visible && v.z > -1 && v.z < 1 && Math.abs(v.x) <= 1 && Math.abs(v.y) <= 1;
      el.style.visibility = on ? 'visible' : 'hidden';
      if (on) el.style.transform = `translate(${Math.round((v.x + 1) * 0.5 * this.width)}px, ${Math.round((1 - v.y) * 0.5 * this.height)}px)`;
    };
    if (this.coord) for (const l of this.coord.labels) place(l.el, l.at, true);
    const letters = (labels, tc, proxy) => {
      if (!labels) return;
      const handles = tc && tc.object ? this._gizmoHandles(false, tc) : [];
      for (const axis of ['X', 'Y', 'Z']) {
        const handle = handles.find((h) => h.handle === axis);
        const el = labels[axis];
        if (!handle || !handle.visible || !tc[`show${axis}`]) {
          el.style.visibility = 'hidden';
          continue;
        }
        place(el, new THREE.Vector3(...AXIS_UNIT[axis]).multiplyScalar(HANDLE_LABEL_AT * handle.scale).add(proxy.position), true);
      }
    };
    letters(this.gizmoLabels, this.transform, this.moveProxy);
    letters(this.pipeGizmoLabels, this.pipeTransform, this.pipeProxy);
  }

  // ---------------------------------------------------------------- input

  _bindPointer() {
    let down = null;
    this.canvas.addEventListener('pointerdown', (e) => {
      this._gizmoGesture = false; // set again by the gizmo's mouseDown when this press grabs a handle
      this._pointerDown = e.pointerId;
      down = { x: e.clientX, y: e.clientY, button: e.button, gizmo: this._overGizmo() };
      this.tooltip.hidden = true;
    });
    this.canvas.addEventListener('pointerup', (e) => {
      this._pointerDown = null;
      if (!down || down.button !== 0 || down.gizmo || this._gizmoGesture) { down = null; return; } // gizmo gestures never select
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
      down = null;
      if (moved > 4) return;
      const hit = this._pickHit(e.clientX, e.clientY);
      const rec = hit ? hit.rec : null;
      if (this.cb.onPick) {
        this.cb.onPick(rec ? {
          semanticId: rec.semanticId, componentId: rec.id, additive: e.shiftKey || e.metaKey,
          point: hit.point, section: this._sectionAt(rec, hit.point),
        } : null);
      }
    });
    this.canvas.addEventListener('pointercancel', () => {
      down = null;
      this._pointerDown = null; // the browser already released the pointer
      this._cancelLocal('pointer-cancel');
      this._pipeAbort('pointer-cancel');
    });
    this.canvas.addEventListener('dblclick', (e) => {
      if (this._gizmoGesture || this._overGizmo() || this.moveLocal || this.pipeLocal) return;
      const rec = this._pick(e.clientX, e.clientY);
      if (!rec) return;
      const ids = new Set(this.componentsFor(rec.semanticId));
      const dir = this.camera.position.clone().sub(this.controls.target);
      this._fit(this._contentBox(ids), dir, 1.3, true);
    });
    let pending = null;
    this.canvas.addEventListener('pointermove', (e) => {
      if (e.buttons) return;
      pending = { x: e.clientX, y: e.clientY };
      if (this.hoverScheduled) return;
      this.hoverScheduled = true;
      requestAnimationFrame(() => {
        this.hoverScheduled = false;
        if (!pending || this.disposed) return;
        const overGizmo = this._overGizmo();
        this._yieldLabels(overGizmo);
        const hit = overGizmo ? null : this._pickHit(pending.x, pending.y);
        const rec = hit ? hit.rec : null;
        if (rec !== this.hovered) {
          const previous = this.hovered;
          this.hovered = rec;
          if (previous) this._applyLook(previous);
          if (rec) this._applyLook(rec);
          this.dirty = true;
        }
        this._showTip(rec, pending.x, pending.y, hit ? this._sectionAt(rec, hit.point) : null);
        if (overGizmo) this.canvas.style.cursor = 'grab';
      });
    });
    this.canvas.addEventListener('pointerleave', () => {
      pending = null;
      this._yieldLabels(false);
      const previous = this.hovered;
      this.hovered = null;
      if (previous) this._applyLook(previous);
      this.tooltip.hidden = true;
      this.dirty = true;
    });
    // A label drawn over a gizmo handle steps aside, so the next press reaches the handle.
    this.labelLayer.addEventListener('pointermove', (e) => {
      if (e.buttons || !this._gizmoUnder(e.clientX, e.clientY)) return;
      this._yieldLabels(true);
      this.canvas.style.cursor = 'grab';
    });
  }

  _showTip(rec, x, y, section = null) {
    if (!rec) {
      this.tooltip.hidden = true;
      this.canvas.style.cursor = '';
      return;
    }
    const r = this.host.getBoundingClientRect();
    this.canvas.style.cursor = 'pointer';
    this.tooltip.hidden = false;
    this.tooltip.replaceChildren();
    const strong = document.createElement('b');
    strong.textContent = rec.tag || rec.semanticId;
    const span = document.createElement('span');
    span.textContent = ` ${rec.key}${section ? ` · section ${section.segmentIndex + 1}` : ''}${rec.ghost ? ' · previous view' : ''}${rec.displayOnly ? ' · display only' : ''}`;
    this.tooltip.append(strong, span);
    this.tooltip.style.transform = `translate(${Math.round(x - r.left + 14)}px, ${Math.round(y - r.top + 12)}px)`;
  }

  _pick(clientX, clientY) {
    const hit = this._pickHit(clientX, clientY);
    return hit ? hit.rec : null;
  }

  /** The first non-ghost component under a client point and the world hit point: {rec, point} or null. */
  _pickHit(clientX, clientY) {
    if (!this.pickables.length) return null;
    const rect = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const hits = this.raycaster.intersectObjects(this.pickables, false);
    let fallback = null;
    for (const hit of hits) {
      const rec = hit.object.userData.record;
      if (!rec) continue;
      // the hit on the displayed mesh, back in the scene's world metres (a move preview offsets meshes)
      const point = hit.point.clone().sub(hit.object.position).toArray();
      if (!rec.ghost && !rec.pipeFaded) return { rec, point };
      fallback = fallback || { rec, point };
    }
    return fallback;
  }

  // ---------------------------------------------------------------- frame loop

  resize() {
    const w = Math.max(1, this.host.clientWidth);
    const h = Math.max(1, this.host.clientHeight);
    if (w === this.width && h === this.height) return;
    this.width = w;
    this.height = h;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.dirty = true;
  }

  _loop(now) {
    if (this.disposed || !this.active) { // paused: setActive(true) restarts the loop
      this.rafId = null;
      return;
    }
    this.rafId = requestAnimationFrame(this._loop);
    if (this.tween) {
      const t = Math.min(1, (now - this.tween.start) / this.tween.duration);
      const k = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      this.camera.position.lerpVectors(this.tween.fromPos, this.tween.toPos, k);
      this.controls.target.lerpVectors(this.tween.fromTarget, this.tween.toTarget, k);
      if (t >= 1) this.tween = null;
      this.dirty = true;
    }
    // OrbitControls re-derives the position from spherical coordinates every frame; on idle
    // frames that round trip adds ~1e-14 m of drift. Keep the exact camera unless it moved.
    const held = this._held || (this._held = new THREE.Vector3());
    held.copy(this.camera.position);
    this.controls.update();
    if (this.camera.position.distanceToSquared(held) < 1e-18) this.camera.position.copy(held);
    if (this.flashing.size) {
      for (const rec of this.flashing) {
        const t = (now - rec.flashStart) / 1600;
        if (t >= 1 || rec.ghost) {
          if (rec.flashMaterial) rec.flashMaterial.dispose();
          rec.flashMaterial = null;
          this.flashing.delete(rec);
        } else {
          if (!rec.flashMaterial) {
            rec.flashMaterial = rec.base.clone();
            rec.flashMaterial.emissive = AMBER.clone();
          }
          rec.flashMaterial.emissiveIntensity = 0.55 * (1 - t) * (1 - t);
        }
        this._applyLook(rec);
      }
      this.dirty = true;
    }
    if (!this.dirty || document.hidden) return;
    this.dirty = false;
    const hidden = [this.edgeGroup, this.markers, this.helpers];
    if (this.transform) hidden.push(this.transform.getHelper());
    if (this.pipeTransform) hidden.push(this.pipeTransform.getHelper());
    for (const rec of this.components.values()) if (rec.ghost) hidden.push(rec.mesh);
    this.studio.beforeRender(hidden);
    this.renderer.render(this.scene, this.camera);
    this._updateLabels();
    this._updateAxisLabels();
    this.frames = (this.frames || 0) + 1;
  }

  /** Client coordinates of a component's bounding-box centre, or null (automated checks). */
  screenPointOf(componentId) {
    const rec = this.components.get(componentId);
    if (!rec) return null;
    const box = new THREE.Box3().setFromObject(rec.mesh);
    const v = box.getCenter(new THREE.Vector3()).project(this.camera);
    if (v.z < -1 || v.z > 1) return null;
    const r = this.canvas.getBoundingClientRect();
    return { x: r.left + ((v.x + 1) / 2) * r.width, y: r.top + ((1 - v.y) / 2) * r.height };
  }

  /** Read-only facts for automated checks (no engineering data is derived from these). */
  debugInfo() {
    let ghosts = 0;
    for (const rec of this.components.values()) if (rec.ghost) ghosts += 1;
    const visibleLabels = [...this.labels.values()].filter((e) => e.el.style.visibility === 'visible').map((e) => e.text);
    const self = this;
    return {
      components: this.components.size, ghosts, selected: [...this.selected].sort(), source: this.source || null,
      camera: this.cameraState(), frames: this.frames || 0, labelsVisible: visibleLabels, labelMode: this.labelMode,
      triangles: [...this.components.values()].reduce((n, r) => n + r.triangles, 0),
      // computed only when read (pane footers call debugInfo often and need none of this)
      get coordinates() { return self._coordinateFacts(); },
      get move() { return self._moveFacts(); },
    };
  }

  _coordinateFacts() {
    const c = this.coord;
    return {
      on: this.coordinatesOn,
      referencePlane: this.studio.referencePlane,
      floorZ: this.studio.floorZ,
      grid: c ? { min: c.extent.min.slice(), max: c.extent.max.slice(), minor: c.extent.minor, major: c.extent.major, zTop: c.extent.zTop } : null,
      axisLabels: c ? c.labels.map((l) => ({ text: l.el.textContent, at: l.at.toArray(), visible: l.el.style.visibility === 'visible' })) : [],
      readout: this.readout ? JSON.parse(JSON.stringify(this.readout)) : null,
      readoutText: this.readoutEl.hidden ? null : this.readoutEl.textContent,
    };
  }

  _moveFacts() {
    const facts = this.moveSnapshot();
    const id = this.preview ? this.preview.entityId : null;
    const components = [];
    const edges = [];
    for (const rec of this.components.values()) {
      const m = rec.mesh.position;
      const e = rec.edges.position;
      if (!m.lengthSq() && !e.lengthSq() && rec.semanticId !== id) continue;
      components.push({ id: rec.id, semanticId: rec.semanticId, offset: m.toArray() });
      edges.push({ id: rec.id, offset: e.toArray() });
    }
    const labels = [];
    for (const [labelId, entry] of this.labels) {
      if (!entry.offset && labelId !== id) continue;
      const anchor = entry.anchor.toArray();
      labels.push({ id: labelId, anchor, displayed: entry.offset ? applyDelta(anchor, entry.offset) : anchor, preview: entry.el.dataset.preview || null });
    }
    const ports = [];
    for (const port of this.ports.values()) {
      if (port.entity_id !== id && !this.portPreview.has(port.id)) continue;
      ports.push({ id: port.id, base: Array.isArray(port.position_m) ? port.position_m.slice() : null, preview: this.portPreview.get(port.id) || null });
    }
    const rect = this.canvas.getBoundingClientRect();
    const handles = this._gizmoHandles(true).map((h) => ({
      handle: h.handle, visible: h.visible, hittable: h.hittable, world: h.world,
      x: rect.left + ((h.ndc[0] + 1) / 2) * rect.width, y: rect.top + ((1 - h.ndc[1]) / 2) * rect.height,
    }));
    const pipeHandles = this.pipeTransform ? this._gizmoHandles(true, this.pipeTransform).map((h) => ({
      handle: h.handle, visible: h.visible && this.pipeTransform[`show${h.handle}`] !== false && h.handle.length === 1, hittable: h.hittable, world: h.world,
      x: rect.left + ((h.ndc[0] + 1) / 2) * rect.width, y: rect.top + ((1 - h.ndc[1]) / 2) * rect.height,
    })).filter((h) => h.handle.length === 1) : [];
    const markers = this.markers.children.map((a) => ({ portId: a.userData.portId, position: a.position.toArray() }));
    const capture = this._pointerDown != null && this.canvas.hasPointerCapture ? this.canvas.hasPointerCapture(this._pointerDown) : false;
    let gizmoOrigin = null;
    if (this.transform && this.transform.object) {
      const v = this.moveProxy.position.clone().project(this.camera);
      gizmoOrigin = { world: this.moveProxy.position.toArray(), x: rect.left + ((v.x + 1) / 2) * rect.width, y: rect.top + ((1 - v.y) / 2) * rect.height };
    }
    return {
      ...facts, components, edges, labels, ports, markers, handles, gizmoOrigin, ghosts: this.ghostGroup.children.length, pipe: this.pipeSnapshot(), pipeHandles,
      gizmoLabels: this.gizmoLabels ? Object.fromEntries(Object.entries(this.gizmoLabels).map(([k, el]) => [k, el.style.visibility === 'visible'])) : null,
      pointerCaptured: capture, escapeListener: !!this._escapeBound, labelsYield: !!this._labelsYield,
    };
  }
}
