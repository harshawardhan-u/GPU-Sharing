// fed_model.js -- builds the SAME little network as fed_model.py, in TensorFlow.js,
// and converts between the model's weights and the flat float32 array sent over
// the network. Training runs on the device's GPU via the WebGL backend.

function buildModel(layers) {
  const model = tf.sequential();
  layers.forEach(([inDim, outDim], i) => {
    model.add(tf.layers.dense({
      units: outDim,
      inputShape: i === 0 ? [inDim] : undefined,
      activation: i === layers.length - 1 ? "softmax" : "relu",
      kernelInitializer: "glorotUniform",
      biasInitializer: "zeros",
    }));
  });
  return model;
}

// Flat array order matches fed_model.py: for each layer, kernel [in,out] then bias [out].
function setFlatWeights(model, flat, layers) {
  const tensors = [];
  let k = 0;
  for (const [inDim, outDim] of layers) {
    tensors.push(tf.tensor2d(flat.subarray(k, k + inDim * outDim), [inDim, outDim])); k += inDim * outDim;
    tensors.push(tf.tensor1d(flat.subarray(k, k + outDim))); k += outDim;
  }
  model.setWeights(tensors);
  tensors.forEach((t) => t.dispose());
}

async function getFlatWeights(model) {
  const ws = model.getWeights();
  const arrays = await Promise.all(ws.map((w) => w.data()));
  let total = 0;
  for (const a of arrays) total += a.length;
  const flat = new Float32Array(total);
  let k = 0;
  for (const a of arrays) { flat.set(a, k); k += a.length; }
  return flat;
}

function flatSize(layers) {
  return layers.reduce((s, [i, o]) => s + i * o + o, 0);
}
