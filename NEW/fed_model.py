"""
fed_model.py -- the tiny neural network shared by the federated-learning demo.

The SAME network runs two places:
  * on every phone, in the browser, trained with TensorFlow.js on the phone's GPU;
  * here in NumPy, so the coordinator can score each phone's work (to catch
    poisoned updates) and measure the shared model's accuracy on a test set.

It is a small multi-layer perceptron for MNIST handwritten digits:
    784 inputs (28x28 pixels)  ->  128 hidden (ReLU)  ->  10 outputs (softmax)
Weights travel over the network as one flat array of float32, in this order:
    W1 [784x128], b1 [128], W2 [128x10], b2 [10]   (101,770 numbers, ~407 KB)
"""

import numpy as np

LAYERS = [(784, 128), (128, 10)]       # (in, out) for each dense layer
INPUT = 784
CLASSES = 10


def flat_size():
    return sum(i * o + o for i, o in LAYERS)


def init_flat(seed=0):
    """He-initialised starting weights, as one flat float32 array."""
    rng = np.random.default_rng(seed)
    parts = []
    for i, o in LAYERS:
        parts.append((rng.standard_normal((i, o)) * np.sqrt(2.0 / i)).astype(np.float32).ravel())
        parts.append(np.zeros(o, np.float32))
    return np.concatenate(parts)


def unpack(flat):
    """Flat array -> [(W1, b1), (W2, b2)]."""
    flat = np.asarray(flat, np.float32)
    out = []
    k = 0
    for i, o in LAYERS:
        W = flat[k:k + i * o].reshape(i, o); k += i * o
        b = flat[k:k + o]; k += o
        out.append((W, b))
    return out


def forward(flat, X):
    """Return class probabilities for inputs X [N, 784] in 0..1."""
    (W1, b1), (W2, b2) = unpack(flat)
    h = np.maximum(0, X @ W1 + b1)
    z = h @ W2 + b2
    z = z - z.max(1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(1, keepdims=True)


def accuracy(flat, X, y):
    return float((forward(flat, X).argmax(1) == y).mean())


def predict(flat, x784):
    """One image -> (digit, probabilities)."""
    p = forward(flat, np.asarray(x784, np.float32).reshape(1, 784))[0]
    return int(p.argmax()), p.tolist()


def to_bytes(flat):
    return np.asarray(flat, np.float32).tobytes()


def from_bytes(buf):
    return np.frombuffer(buf, np.float32).copy()
