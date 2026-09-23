import torch
import time

print("Checking for GPU...")
# Check if the Host's RTX 4060 is accessible inside the container
if torch.cuda.is_available():
    device = torch.device("cuda")
    print(f"🔥 Success! Found GPU: {torch.cuda.get_device_name(0)}")
else:
    device = torch.device("cpu")
    print("😢 No GPU found, using CPU instead.")

print("\nInitializing a heavy Matrix Multiplication task...")
# Create two massive matrices directly on the GPU memory
x = torch.randn(5000, 5000, device=device)
y = torch.randn(5000, 5000, device=device)

start_time = time.time()
print("Computing product on the RTX 4060...")
result = torch.matmul(x, y)
end_time = time.time()

print(f"✅ Finished in {end_time - start_time:.4f} seconds!")