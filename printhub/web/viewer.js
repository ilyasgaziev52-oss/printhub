// 3D-просмотр модели на столе принтера (three.js)
import * as THREE from 'three';
import { STLLoader } from './vendor/STLLoader.js';
import { OrbitControls } from './vendor/OrbitControls.js';

export class ModelViewer {
  constructor(container) {
    this.container = container;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 4 / 3, 0.1, 10000);
    this.camera.up.set(0, 0, 1);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 1.6));
    const dir = new THREE.DirectionalLight(0xffffff, 1.6);
    dir.position.set(1, -1.5, 2);
    this.scene.add(dir);
    this.bedGroup = new THREE.Group();
    this.scene.add(this.bedGroup);
    this.mesh = null;
    this.overhangMesh = null;
    this.bed = [220, 220, 250];
    this.setBed(this.bed);

    this._resize = () => this.resize();
    new ResizeObserver(this._resize).observe(container);
    this.resize();
    const loop = () => {
      this._raf = requestAnimationFrame(loop);
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    };
    loop();
  }

  resize() {
    const w = this.container.clientWidth || 400;
    const h = this.container.clientHeight || 300;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  setBed(size) {
    const [x, y, z] = size.map((v) => (v && v > 0 ? v : 220));
    this.bed = [x, y, z];
    this.bedGroup.clear();
    const plate = new THREE.Mesh(
      new THREE.PlaneGeometry(x, y),
      new THREE.MeshStandardMaterial({ color: 0x808a96, transparent: true, opacity: 0.25, side: THREE.DoubleSide }));
    plate.position.set(x / 2, y / 2, -0.01);
    this.bedGroup.add(plate);
    const grid = new THREE.GridHelper(Math.max(x, y), Math.round(Math.max(x, y) / 10), 0x667085, 0x98a2b3);
    grid.rotation.x = Math.PI / 2;
    grid.position.set(x / 2, y / 2, 0);
    grid.material.opacity = 0.35;
    grid.material.transparent = true;
    this.bedGroup.add(grid);
    const box = new THREE.Box3Helper(new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(x, y, z)), 0x5b8cff);
    box.material.opacity = 0.3;
    box.material.transparent = true;
    this.bedGroup.add(box);
  }

  async loadUrl(url, { overhangAngle = 45 } = {}) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(await resp.text());
    const buf = await resp.arrayBuffer();
    const geom = new STLLoader().parse(buf);
    this.show(geom, overhangAngle);
  }

  show(geom, overhangAngle = 45) {
    if (this.mesh) { this.scene.remove(this.mesh); this.mesh.geometry.dispose(); }
    if (this.overhangMesh) { this.scene.remove(this.overhangMesh); this.overhangMesh.geometry.dispose(); }
    geom.computeBoundingBox();
    const bb = geom.boundingBox;
    // кладём на стол и ставим в центр
    const cx = (bb.min.x + bb.max.x) / 2, cy = (bb.min.y + bb.max.y) / 2;
    geom.translate(this.bed[0] / 2 - cx, this.bed[1] / 2 - cy, -bb.min.z);
    geom.computeVertexNormals();
    const mat = new THREE.MeshStandardMaterial({ color: 0x4f86f7, metalness: 0.05, roughness: 0.6, flatShading: false });
    this.mesh = new THREE.Mesh(geom, mat);
    this.scene.add(this.mesh);

    // подсветка нависаний
    const pos = geom.getAttribute('position');
    const thr = -Math.sin(overhangAngle * Math.PI / 180);
    const tmpA = new THREE.Vector3(), tmpB = new THREE.Vector3(), tmpC = new THREE.Vector3(), n = new THREE.Vector3();
    const over = [];
    for (let i = 0; i < pos.count; i += 3) {
      tmpA.fromBufferAttribute(pos, i); tmpB.fromBufferAttribute(pos, i + 1); tmpC.fromBufferAttribute(pos, i + 2);
      n.subVectors(tmpC, tmpB).cross(tmpA.clone().sub(tmpB)).normalize();
      const minZ = Math.min(tmpA.z, tmpB.z, tmpC.z);
      if (n.z < thr && minZ > 0.2) {
        over.push(tmpA.x, tmpA.y, tmpA.z, tmpB.x, tmpB.y, tmpB.z, tmpC.x, tmpC.y, tmpC.z);
      }
    }
    if (over.length) {
      const g2 = new THREE.BufferGeometry();
      g2.setAttribute('position', new THREE.Float32BufferAttribute(over, 3));
      g2.computeVertexNormals();
      this.overhangMesh = new THREE.Mesh(g2, new THREE.MeshStandardMaterial({
        color: 0xe5484d, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 }));
      this.scene.add(this.overhangMesh);
    }
    this.fit();
  }

  setOverhangsVisible(v) { if (this.overhangMesh) this.overhangMesh.visible = v; }

  fit() {
    const box = new THREE.Box3().setFromObject(this.mesh || this.bedGroup);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.x, size.y, size.z, 20);
    this.controls.target.copy(center);
    this.camera.position.set(center.x + radius * 1.4, center.y - radius * 1.8, center.z + radius * 1.2);
    this.camera.near = radius / 100;
    this.camera.far = radius * 100;
    this.camera.updateProjectionMatrix();
  }

  snapshot(size = 256) {
    this.renderer.render(this.scene, this.camera);
    const src = this.renderer.domElement;
    const c = document.createElement('canvas');
    c.width = size; c.height = size;
    const ctx = c.getContext('2d');
    const s = Math.min(src.width, src.height);
    ctx.drawImage(src, (src.width - s) / 2, (src.height - s) / 2, s, s, 0, 0, size, size);
    return c.toDataURL('image/png');
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    this.renderer.dispose();
    this.container.innerHTML = '';
  }
}
