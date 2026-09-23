import torch
import time

print("=========================================")
print("🔥 INITIATING GPU TENSOR STRESS TEST 🔥")
print("=========================================")

# Check for GPU
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
if torch.cuda.is_available():
    print(f"✅ GPU Detected: {torch.cuda.get_device_name(0)}")
else:
    print("❌ No GPU Detected. Running on CPU (This will be slow...)")

# Generate massive tensors (10,000 x 10,000 matrices = 100 million floats each)
matrix_size = 10000
print(f"\nGeneraing two {matrix_size}x{matrix_size} matrices...")
tensor_a = torch.randn(matrix_size, matrix_size, device=device)
tensor_b = torch.randn(matrix_size, matrix_size, device=device)

print("Multiplying 100 million data points... hold on...")
start_time = time.time()

# Perform Matrix Multiplication
result_tensor = torch.matmul(tensor_a, tensor_b)

# Wait for GPU to finish calculating
if torch.cuda.is_available():
    torch.cuda.synchronize()

end_time = time.time()
print(f"⏱️ Computation completed in: {end_time - start_time:.4f} seconds")

# Save the resulting tensor to prove our File Sync works
print("\n💾 Saving resulting tensor weights to 'stress_test_model.pt'...")
torch.save(result_tensor, "stress_test_model.pt")

print("✅ Job Complete! Syncing files back to client...")