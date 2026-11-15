// The bundled spacecraft uses indexed triangle meshes, node matrices and solid material colors.
const vertexShader = `#version 300 es
in vec3 position;
uniform vec3 angles;
uniform float aspect;
out vec3 surface;
void main() {
  float cx = cos(angles.x), sx = sin(angles.x);
  float cy = cos(angles.y), sy = sin(angles.y);
  float cz = cos(angles.z), sz = sin(angles.z);
  vec3 p = vec3(position.x * cy + position.z * sy, position.y, position.z * cy - position.x * sy);
  p = vec3(p.x, p.y * cx - p.z * sx, p.y * sx + p.z * cx);
  surface = vec3(p.x * cz - p.y * sz, p.x * sz + p.y * cz, p.z);
  gl_Position = vec4(surface.x, surface.y * aspect, -surface.z * .6, 1.0 + surface.z * -.18);
}`;

const fragmentShader = `#version 300 es
precision highp float;
in vec3 surface;
uniform vec3 material;
out vec4 pixel;
void main() {
  vec3 normal = normalize(cross(dFdx(surface), dFdy(surface)));
  float light = .43 + .57 * max(dot(normal, normalize(vec3(-.35, .65, .8))), 0.0);
  pixel = vec4(pow(clamp(material * light + .025, 0.0, 1.0), vec3(1.0 / 2.2)), 1.0);
}`;

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
  return shader;
}

function readMeshes(contents) {
  const header = new DataView(contents);
  const jsonLength = header.getUint32(12, true);
  if (header.getUint32(0, true) !== 0x46546c67 || header.getUint32(4, true) !== 2 ||
      header.getUint32(16, true) !== 0x4e4f534a || header.getUint32(24 + jsonLength, true) !== 0x004e4942) {
    throw new Error("Invalid Terra model");
  }
  const model = JSON.parse(new TextDecoder().decode(new Uint8Array(contents, 20, jsonLength)));
  const binaryOffset = 28 + jsonLength;
  const identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  const parts = [];
  function read(index, types, components) {
    const accessor = model.accessors[index];
    const view = model.bufferViews[accessor.bufferView];
    const ArrayType = types[accessor.componentType];
    if (!ArrayType || view.byteStride) throw new Error("Unsupported Terra mesh layout");
    return new ArrayType(contents, binaryOffset + (view.byteOffset ?? 0) + (accessor.byteOffset ?? 0), accessor.count * components);
  }

  function visit(index, parent) {
    const node = model.nodes[index];
    const matrix = node.matrix ? node.matrix.map((_, i) => {
      const col = i - i % 4;
      const row = i % 4;
      return [0, 1, 2, 3].reduce((sum, k) => sum + parent[row + k * 4] * node.matrix[col + k], 0);
    }) : parent;
    if (node.mesh != null) {
      for (const primitive of model.meshes[node.mesh].primitives) {
        const indices = read(primitive.indices, { 5123: Uint16Array, 5125: Uint32Array }, 1);
        const source = read(primitive.attributes.POSITION, { 5126: Float32Array }, 3);
        const vertices = new Float32Array(source.length);
        for (let i = 0; i < source.length; i += 3) {
          for (let axis = 0; axis < 3; axis++) {
            const n = matrix[axis] * source[i] + matrix[4 + axis] * source[i + 1] +
              matrix[8 + axis] * source[i + 2] + matrix[12 + axis];
            vertices[i + axis] = n;
            min[axis] = Math.min(min[axis], n);
            max[axis] = Math.max(max[axis], n);
          }
        }
        const color = model.materials[primitive.material].pbrMetallicRoughness.baseColorFactor.slice(0, 3);
        parts.push({ indices, vertices, color });
      }
    }
    for (const child of node.children ?? []) visit(child, matrix);
  }
  for (const node of model.scenes[model.scene ?? 0].nodes) visit(node, identity);
  const scale = 1.8 / Math.max(...max.map((v, axis) => v - min[axis]));
  const center = min.map((v, axis) => (v + max[axis]) / 2);
  for (const { vertices } of parts) {
    for (let i = 0; i < vertices.length; i++) vertices[i] = (vertices[i] - center[i % 3]) * scale;
  }
  return parts;
}

export async function loadTerraModel(canvas) {
  const gl = canvas.getContext("webgl2", { alpha: true, antialias: true });
  if (!gl) return null;
  const response = await fetch("/static/assets/satellites/terra.glb");
  if (!response.ok) throw new Error("Terra model unavailable");
  const parts = readMeshes(await response.arrayBuffer());
  const program = gl.createProgram();
  for (const [type, source] of [[gl.VERTEX_SHADER, vertexShader], [gl.FRAGMENT_SHADER, fragmentShader]]) {
    const shader = compile(gl, type, source);
    gl.attachShader(program, shader);
    gl.deleteShader(shader);
  }
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  gl.useProgram(program);
  gl.enable(gl.DEPTH_TEST);
  const position = gl.getAttribLocation(program, "position");
  const angles = gl.getUniformLocation(program, "angles");
  const aspect = gl.getUniformLocation(program, "aspect");
  const material = gl.getUniformLocation(program, "material");
  const meshes = parts.map(({ vertices, indices, color }) => {
    const mesh = gl.createVertexArray();
    gl.bindVertexArray(mesh);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);
    return { mesh, length: indices.length, type: indices instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT, color };
  });

  let lastBank;
  return {
    draw(bank) {
      const angle = Math.round(bank * 10) / 10;
      if (angle === lastBank) return;
      lastBank = angle;
      const rad = Math.PI / 180;
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.uniform3f(angles, 21 * rad, 8 * rad, (8 - angle) * rad);
      gl.uniform1f(aspect, canvas.width / canvas.height);
      for (const { mesh, length, type, color } of meshes) {
        gl.bindVertexArray(mesh);
        gl.uniform3fv(material, color);
        gl.drawElements(gl.TRIANGLES, length, type, 0);
      }
    },
  };
}
